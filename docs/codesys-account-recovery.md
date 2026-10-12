# 本机 CODESYS Control Win 设备账户恢复

2026-10-09：修改后的凭据测试返回“操作中止，尝试次数过多”，未确认密码是否正确。用户随后说明已遗忘设备账户密码。当前未执行账户重置。

官方说明：登录锁默认在多次失败后触发；重启 Runtime 可解除临时锁，不能恢复已遗忘的密码。具体锁定时间由设备策略决定。[CODESYS 设备用户管理](https://content.helpme-codesys.com/en/Security/_sec_user_management_plc.html)

CODESYS 厂商支持人员给出的 Control Win 本机恢复步骤是：停止 SoftPLC，移除用户管理 CSV 配置，再启动并创建新的设备账户。这里只处理账户配置，不能清空整个 Runtime 目录。[厂商支持人员的恢复步骤](https://forge.codesys.com/forge/talk/Engineering/thread/5038eed7e2/5b1d/6297/873c/403b)

本机服务为 `CODESYS Control Win V3 - x64`，可执行文件 `G:\codeesys\GatewayPLC\CODESYSControlService.exe`，以 LocalSystem 运行。已读取的安装配置指定 `%APPDATA%\CODESYS\CODESYSControlWinV3x64\635CD56B`，对应的 SYSTEM 账户目录为：

```text
C:\Windows\System32\config\systemprofile\AppData\Roaming\CODESYS\CODESYSControlWinV3x64\635CD56B
```

当前普通账户读取该系统目录返回访问拒绝，尚未确认里面实际的账户文件。已准备 `tools/reset-codesys-device-users.ps1`，必须在 64 位管理员 PowerShell 运行。脚本核对本机服务、安装配置与目标目录，只允许处理固定的 `.UserDatabase.csv`、`.GroupDatabase.csv`、`.UserMgmtRightsDB.csv` 及其 `.csv_` 备份。若未找到活动的 `.UserDatabase.csv`，脚本中止，不猜测其他数据库或修改服务。

只查看目录和账户文件清单：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\phd\project\digital-twin-factoryi-o\tools\reset-codesys-device-users.ps1"
```

备份并重置账户文件：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\phd\project\digital-twin-factoryi-o\tools\reset-codesys-device-users.ps1" -ResetUsers
```

重置模式停止该 Runtime 服务，将每个账户文件复制到新的项目 artifacts 目录并核对 SHA256，随后移走原账户文件，恢复原先运行的服务。脚本不移动或删除 PLC 程序、启动应用、场景或工程，不设置默认密码，不创建新账户。启动现有 Runtime 时，原启动应用可能按自身配置继续执行。新账户需要在 CODESYS 中扫描并连接本地设备后，按首次初始化提示创建，密码需满足当前设备策略。

脚本已通过 PowerShell 语法检查；管理员目录检查、重置和新账户创建尚未执行验证。发生异常时读取报告中的实际移动文件列表和备份路径，不能把脚本中途失败标记为重置完成。完整设备原始复位（Reset Origin Device）有更广泛的影响，当前恢复流程不调用它。

后续实测：管理员执行脚本时进入了预期 SYSTEM 目录，但未找到白名单中的 `.UserDatabase.csv`，脚本返回错误且未移动任何文件或改变服务状态（`artifacts/codesys-user-reset-20261009-070519-fbbf9cba/report.json`）。该脚本尚未验证为本机可用的重置办法，当前版本不应重复执行作为默认恢复流程。用户随后更新了设备凭据，CLI 与项目 MCP 在线登录均成功；这证明新凭据有效，不证明重置脚本执行成功。
