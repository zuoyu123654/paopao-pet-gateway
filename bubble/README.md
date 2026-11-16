# 上游代码声明

`speech_bubble.py`、`speech_bubble_text.py` 来自
[dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 的 `pet/` 目录，
以 **MIT** 协议发布，版权归原作者所有。

我们**只改了导入路径**（放进 `bubble/` 这个包里），其余保持原样，方便以后跟上游对照升级。
如果上游更新了气泡实现，把这两个文件覆盖过来即可；我们的 `bubble_host.py` 用的是公开接口
（`show_text` / `reflow` / `reposition` / `set_style` / `set_text_scale`），一般不用改。
