# 白龙桌宠开发交接

## 换电脑继续

从 `lll135/codex-bailong-desktop-pet` 的 `codex/antigravity-desktop` 分支继续。`e3fce71` 是本次交接前的源码提交，包含 Antigravity 联动、来源切换，以及最近两项界面修复。

```powershell
git clone --branch codex/antigravity-desktop https://github.com/lll135/codex-bailong-desktop-pet.git
cd codex-bailong-desktop-pet
git status --short --branch
```

如另一台电脑已经有检出，先检查工作区，再 `git fetch origin` 并切换到跟踪 `origin/codex/antigravity-desktop` 的本地分支。开工读 [AGENTS.md](AGENTS.md)、[开发与构建](docs/DEVELOPMENT.md) 和 [维护与防错规范](维护与防错规范.md)。同级 DSH 工程单独维护。

## 版本和验收

- 当前 Windows 公开稳定版为 **[v1.10.0](docs/releases/v1.10.0.md)**，已从 `codex/antigravity-desktop`（`7769be1`）公开发布至 [lll135/codex-bailong-desktop-pet Release v1.10.0](https://github.com/lll135/codex-bailong-desktop-pet/releases/tag/v1.10.0)。本次构建使用无 `--system-site-packages` 的隔离环境 `build\release-venv`（安装 `requirements-enhanced.txt` 官方 wheel，含 PySide6 TLS OpenSSL 后端与 `cryptography==50.0.2`），依次通过 165 个模块 1,927 项全量回归、`check_bundle.py` 编译核对、冻结 TLS（含仅保留系统 `PATH` 的独立核验）、4 档缩放原生交互、15 项在线下载与 6 项离线自举 C# 校验，以及全部 6 个 Release 附件的匿名公开流式下载与 SHA-256 一致性核验（素材下载仍指向 `Wunsch2048/codex-bailong-desktop-pet` `v1.3.0`）。
- 仓库此前记录的 Windows 公开稳定版为 [v1.9.0](docs/releases/v1.9.0.md)。公开版本和本地实装继续按 AGENTS.md 各自递增。
- 原电脑实装 v1.10.2，v1.10.1 可回退。v1.10.2 含 Antigravity 联动、主题色箭头和统一双来源标题。最后一次本地验收通过 69 项定向回归，并验证了实装启动、设定保留和回退包。
- 分支后续已修「运行／运行与连接」菜单名称，以及普通卡片的额度百分比和重置图标重叠。设置检查 26 项、额度检查 46 项通过，额度问题在 150% 缩放下用 `100%` 和 `4时56分` 复测。两项修复仍待构建和实装。沿用原电脑 v1.10.2 基线时，它们作为修复版升到 v1.10.3。
- 原电脑的 `本地实装/最终验收.json`、`本地实装/Antigravity联动验收.json`、构建目录、安装备份及真实用户资料都在本地。新电脑重新生成自己的验收记录。

## Windows 构建与实装

从仓库根目录运行。使用 Windows x64 和 Python 3.12，按 [requirements-enhanced.txt](requirements-enhanced.txt) 安装依赖。

```powershell
py -3.12 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r requirements-enhanced.txt
```

选择本次相关的测试模块。比如下一步处理 Antigravity 用量和卡片展示，可先跑下面的源码检查。`tools/check_source.py` 和构建入口会清理临时目录，按 AGENTS.md 取得当轮允许的清理范围再执行。

```powershell
& $python tools/check_source.py test_antigravity test_antigravity_data test_quota_summary test_compact_bubble
```

仅改程序、动画目录内容一致时，可以复用与 [素材目录](app/material_catalog.json) 匹配的轻量目录。默认缓存位于 `%LOCALAPPDATA%\BailongEnhanced\materials\light\<素材ID>`，安装目录中的轻量素材或已解压的匹配轻量包也可用。传入直接包含 `素材清单.json` 的目录，构建入口逐项校验文件与源码清单。需要从原素材完整构建时，先通过白龙素材设置下载高清资源，运行 `tools/import_installed_assets.py`，再走默认构建入口。细节见 [开发与构建](docs/DEVELOPMENT.md)。

```powershell
$light = Read-Host '与目录匹配的轻量素材目录'
& $python build_local.py --build-only --light-source $light --tests test_settings_presentation test_quota_summary
# 构建验收通过，且源码和包体一致时再执行本地实装。
& $python build_local.py --deploy-verified
```

Windows 默认用户数据目录为 `%LOCALAPPDATA%\BailongEnhanced`，默认程序目录为 `%LOCALAPPDATA%\Programs\BailongEnhanced`。实装前核对本机实际入口和版本。API Key、OAuth、Sidecar 的本地授权配置和真实会话资料留在各自机器。另一台电脑重新登录和配置；DPAPI 加密凭据的可用性与原 Windows 环境绑定。

## Antigravity 联调入口

1. 在新电脑安装并登录 Antigravity 桌面端。此前实际联调版本为 Windows App 2.34.0.0。更换版本后重查 Hook 回执、Sidecar 启动和本地数据库字段。
2. 在白龙设置的「运行与连接」（旧版左侧显示「Codex」）里开启「监听 Antigravity 桌面 App」、配置状态桥接并保存。卡片左上角箭头切换来源，标题打开选中的应用。额度桥接可单独配置。
3. 需要测试白龙闲聊时，进入「聊天设置 → Antigravity」，在 Antigravity 建立白龙专用项目，用该电脑的项目 ID 配置桌面 App 聊天。重点验收一次发送、同会话续聊、取消后不误报完成。Sidecar 的发送走公开 `agentapi`，额度读取和停止回复目前还依赖 App 内部接口。
4. 日志索引读取 `%USERPROFILE%\.gemini\antigravity\conversations` 下的本机数据库，写入白龙自己的 `antigravity-usage.sqlite3`。另一台电脑会从它可读到的会话建立索引。

## 下一项功能

- 已补齐 Antigravity 桌面端能力支持，并在隔离环境完成单测回归：
  - **历史速率**：依据 Antigravity `gen_metadata` 内置 `chat[12]`（`google.protobuf.Duration` 服务端流式生成耗时），计算单次生成速率及回合输出速率；主卡片底部栏显示 `历史` 与 `tok/s`，悬停弹出 `会话历史速率`（标明「按服务端生成耗时统计」）；日报页恢复「昨日生成均速」、模型均速与小时/按日叠加曲线。
  - **本轮合计与上下文占用**：按会话 `steps.step_type=14` 用户输入边界汇总单轮全部模型请求的四项 Token 与速率，并在「展开卡片 → Token 明细 → 最近请求」中增加「本轮合计」；读取 `chat[9][10]` 的已用上下文与窗口上限展示「上下文占用」进度条。
  - **项目成本与计价**：新增 [antigravity_projects.py](app/antigravity_projects.py)，从 `conversation_summaries.db` 的 `workspace_uris` 映射工作区路径并内置常见公开模型单价基准（未公开发布的模型保留待定价）；大卡片开放「成本」页与项目 → 会话 → 回合下钻，计价设置支持合并 Codex 与 Antigravity 模型列表并跨端触发重算。
  - **周额度周期对比与低额提醒**：按模型组（`quota_observations` 多套餐主键）记录周额度历史，大卡片开放「额度」周期对比页，主卡片恢复 `visible_quota_change` 变动提示；120 秒内新鲜额度低于等于 15% 时按 `(模型组, 窗口, resetsAt)` 去重弹出桌面提醒。
  - **连接诊断、前后台与退出联动**：设置页「运行与连接」显示 Antigravity 四项只读诊断摘要并合入连接检测报告；开启监听后，「仅在 Codex 前台时显示」同步识别 `antigravity.exe`，「Codex 关闭后退出桌宠」等待两个受监听应用均退出后触发；状态图标提示文本与灵动胶囊无障碍文本随来源切换（界面文案不使用中点）。
  - 仍受限于 Antigravity 上游架构的能力：无 Token 实时流式推送（以服务端历史速率替代）、无 Codex Resets 公告流、无公开深链定位单个对话、无对话内自动已读回执。

## Antigravity 桌面 App 联动开发

- Windows 源码已接状态桥接。现有联动设置可配置、停用白龙 Hook，状态监听默认关闭。左上角标题打开所选应用，独立箭头切换来源，正常卡片可显示两端，大卡片显示所选来源。切换偏好独立于现有胶囊快捷键。
- 隔离配置已验证原 Hook 保留、重复配置、停用、PowerShell 到同用户 Windows 命名管道、桌宠关闭时的中性返回，以及双端完成提醒和 Codex 额度刷新隔离。首次检查发现标题重绘重新启用了仍在刷新的按钮，已修为按当前来源和忙碌状态共同判断。
- 已把额度、Token 主卡和日报/模型页接入来源切换。额度支持本机现有 `agy-gemini-pet-bridge` 导出，另提供白龙自身的按需 Sidecar，成功读取后提供模型组切换、真实窗口及倒计时。白龙 Sidecar 仅监听回环并凭本地授权随机值接受请求。真实 App 2.34.0.0 已热加载白龙 Sidecar 并由该链路读出三个模型组。
- Antigravity 会话库只读证据覆盖本机 87 个会话库、66 个日期。曾因整行读取上限漏掉 20 个库，已改为按 SQLite Blob 字段跳读，并核直接/步骤时间、请求去重、旧快照保留。数据写入白龙独立 SQLite 索引，现有应用配置和会话记录保持原状。
- 105 项定向源码测试通过。额外隔离检查覆盖独立 Sidecar 的回环请求、短缓存、空闲零请求和断连，以及真实导出与会话库的卡片显示、模型组选择、日报入口和切回 Codex。未通过正式构建脚本生成发布验收凭据。
- 闲聊源码已提供 Antigravity 服务。白龙本地会话绑定服务，Sidecar 用专用项目和文本配置发起或续聊，工作区 Hook 负责阻止工具调用并记录回合终态。先保存请求身份再发送，状态不明或停止等待后仅重新取回原请求，明确未发出时可重试。隔离联调覆盖停止等待、重新获取及服务绑定，真实 App 联调覆盖两轮真实发送、新建与续聊同一 ID、同轮回复和状态 Hook 思考/完成。
- 回复按请求标识与步骤编号关联。日志明确标截断时，只读回查同会话 `steps` 的同一个已完成步骤，并核对文本前缀；无法匹配时要求在 App 查看。实库抽样覆盖 7 条可恢复的完成回复，另外 4 条在 App 中已清除，正确忽略。首轮 Hook 写入成功之后还核过重复终态替换，PowerShell .NET 调用使用 `NullString` 传无备份参数。
- 闲聊相关 93 项定向检查通过。现有 ChatGPT 与自定义 API 配置和窗口检查继续通过。验证使用隔离目录与替身服务，保留了检查夹具，未执行带批量清理的正式构建。
- 实机首次发送已经由 App 受理，但白龙漏读嵌套事件回执，把回合误判为投递不明。已按发送标识读取 App 原始回执并保留原会话，未补发首条。App 正常停止实际为 `NO_TOOL_CALL`，已在任务和聊天两条链路验证。另读到实际 `conversation_summaries.db` 标题字段，由现有后台监听向卡片补会话名。
- 实机联调保留白龙专用项目、测试会话与独立额度 Sidecar；原 Antigravity 对话与草稿已恢复显示。相关 63 项定向源码检查通过；实际会话标题经现有后台监听到卡片行另行核验。
- 真实 App 2.34 接受 `CancelCascadeInvocation` 后可直接空闲，取消时没有 Stop Hook。白龙的停止按钮现在校验最近输入标识和专用会话后请求 App 停止，通过本地摘要两次确认全空闲，再结束同一待处理请求和忙碌状态。已实测无完成提醒，并在同一会话续聊成功。普通状态监测遇到漏回执，须先观察同一回合确实活跃，再用连续空闲样本静默结束；后台仍运行、旧回合和未知状态不清理。
- v1.10.0 本地构建复用了与当前发布目录完全一致的 45 个轻量素材文件。正式本地入口通过 107 项定向回归、四档 DPI 原生交互和包体校验，再安装已验证的同一包。实装后启用了白龙 Hook 和监听，保留 Codex 为当前卡片来源并确认其他 Hook 条目未变。实装进程响应同用户命名管道，真实 Sidecar 返回三个额度模型组；双向切换和标题分派由源码 Qt 点击验证，桌面自动化未能选中实装的悬浮卡片。
- v1.10.1 把来源按钮的 Windows 原生实心箭头改为共用标题栏绘图中的小号下折线，保持 22 × 26 逻辑像素点击区域。已核七种预设和自定义主题的实时着色，Qt 点击双向切换通过。39 项定向回归通过，版本实装已验收。
- v1.10.2 双来源打开时，Codex 和 Antigravity 标题使用 15 逻辑像素的 Chakra Petch SemiBold。标题以控件实际字体测量，箭头随文字宽度移动。普通和展开卡片已点验双向切换，69 项定向回归通过，版本实装已验收。
- 设置入口使用「运行」，页面标题使用「运行与连接」，覆盖启动退出、状态显示、额度提醒、成本和两端的连接设置。专用选项继续标明所属应用。
- 普通卡片的双额度行实测 `100%` 与 `4时56分` 的重置图标相叠。源码已按实际字号、字形和图标笔画给左右两组分配空间，原卡片宽度保持。
- 桌面端的会话菜单提供会话 ID。当前公开深链处理支持打开 App 和项目，以及预填文本，未取得可定位具体会话的入口。审批、等待输入、长后台任务的真实终态验证，以及精确会话跳转、实时回复、项目成本、回合速率和已读仍待补充。
