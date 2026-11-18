# GPT-SoVITS 两个必须打的补丁（给 AI）

> 这两个补丁**不是可选项**。不打，你会看到：
>
> 1. **第一句念到一半卡住**（整句合成把 API 的事件循环堵死，流式一个字节都发不出去）；
> 2. **并发时 TTS 崩**（多个请求共用同一个模型对象，流式请求的方法被另一个请求改掉，
>    报 `not enough values to unpack (expected 2, got 1)`，连接直接断）。
>
> 补丁是 2026-11-15/16 在本机 GPT-SoVITS **v2pro-20250604** 上改的，两份 diff 都实测
> 能干净套用、套完与线上文件逐字节一致。

## 打之前

- 目录：`<GSV>` = GPT-SoVITS 根目录（`api_v2.py` 所在那层）。
- 先备份（补丁本身不改备份文件）：

```powershell
Copy-Item <GSV>\api_v2.py <GSV>\api_v2.py.bak-before-patch
Copy-Item <GSV>\GPT_SoVITS\TTS_infer_pack\TTS.py <GSV>\GPT_SoVITS\TTS_infer_pack\TTS.py.bak-before-patch
```

## 打补丁

```powershell
cd <GSV>
Copy-Item <网关目录>\gsv_patches\*.patch .
git -c core.autocrlf=false apply api_v2.py.patch
git -c core.autocrlf=false apply TTS.py.patch
```

- 必须带 `-c core.autocrlf=false`：不带的话 git 可能把整个文件换成 CRLF，虽然也能跑，
  但和线上文件就不一致了。
- 没有 git 的环境（Git Bash / WSL 也行）：

```bash
patch -p1 < api_v2.py.patch
patch -p1 < TTS.py.patch
```

- 先干跑检查（不想直接改文件）：

```powershell
git -c core.autocrlf=false apply --check api_v2.py.patch
git -c core.autocrlf=false apply --check TTS.py.patch
```

## 打完怎么确认

```powershell
Select-String <GSV>\api_v2.py -Pattern 'run_in_threadpool', '_pick_cuda_stream'
Select-String <GSV>\GPT_SoVITS\TTS_infer_pack\TTS.py -Pattern 'infer_panel_fn'
```

三样都能搜到就算打上了。然后重启 9882（加载模型 60～90 秒），
用网关 `POST /say {"text":"测试一下，这是一句比较长的话，用来看第一句会不会卡。"}` 验证：
**第一句应该一口气念完，不中间停顿**。

## 版本不对怎么办

diff 是给 `v2pro-20250604` 的。别的版本 `git apply` 报 `patch does not apply` 时：

1. `git -c core.autocrlf=false apply --ignore-whitespace ...` 再试；
2. 还不行就手工照补丁改 3 处（都很小）：
   - `api_v2.py`：`tts_handle` 里 `sr, audio_data = next(tts_generator)` 换成线程池 +
     独立 CUDA 流（见补丁 466 行那段）；
   - `TTS.py`：`run()` 里所有 `self.t2s_model.model.infer_panel = ...` 改成局部变量
     `infer_panel_fn = ...`，下面两处调用跟着改名；
3. 打完必须重跑上面的确认命令，别凭感觉。

## 回滚

```powershell
cd <GSV>
git -c core.autocrlf=false apply -R api_v2.py.patch
git -c core.autocrlf=false apply -R TTS.py.patch
# 或者把 .bak-before-patch 覆盖回去
```

## 为什么值得打

- 第一句：不打时整句合成会独占事件循环，实测流式首字节被拖到 5 秒以上、还会中途冻住；
  打完后首字节 1.1～1.5 秒，整句合成从 9～13 秒降到 2～5 秒（施工单 4.2f）。
- 并发：不打时「识屏 + 聊天 + 弹幕」同时来会直接把 9882 打崩；打完后三种请求可以并存。
