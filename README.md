# Telegram 签到工作台

在自己的 Linux 服务器上集中管理多个 Telegram 帐号的定时签到。提供中文 Web 界面、用户名密码登录，以及命令行管理菜单。

**支持：**多帐号批量任务集合、发送自定义消息、可选发送失败重试、点击回复中的指定文本或回调按钮、校验成功回复、独立 Cron 与时区、手动执行、帐号/任务/全局开关、90 天执行记录、加密会话、备份与更新。

**使用个人 Telegram 帐号登录，需要你自己的 API ID / API Hash；不是 Bot Token。** 每个帐号只需完成一次验证码登录；启用 Telegram 两步验证的帐号还需要输入密码。

<details>
<summary>界面预览（初始空工作台，窄屏布局）</summary>

![签到工作台](docs/dashboard.png)

</details>

## 一键部署

推荐 **Debian 12/13、Ubuntu 24.04+**。需要 systemd、Python 3.11+，服务器必须能连接 GitHub、PyPI 和 Telegram。安装器会用 apt/dnf 安装依赖；旧系统若默认 Python 低于 3.11 会明确停止。

```bash
curl -fsSL https://raw.githubusercontent.com/Jk-z-Box/telegram-checkin-manager/main/install.sh | sudo bash
```

如果已经是 root，且未安装 sudo：

```bash
curl -fsSL https://raw.githubusercontent.com/Jk-z-Box/telegram-checkin-manager/main/install.sh | bash
```

首次安装会显示交互配置向导，依次设置：

1. **监听方式**：仅本机 `127.0.0.1`、公网/局域网 `0.0.0.0`，或者自定义 IPv4/IPv6 地址。
2. **Web 端口**：可自定义 `1–65535`，默认 `8080`。安装的 systemd 服务允许非 root 服务帐号绑定低端口。
3. **浏览器访问地址**：经 Cloudflare 或反向代理访问时，填写完整地址，例如 `https://checkin.example.com`；直接访问服务器 IP 可保留 `off`。
4. **HTTPS Cookie**：是否只允许浏览器经 HTTPS 登录，默认关闭；开启前需要确保 HTTPS 域名可以访问。
5. **开机自启**：默认开启。关闭后仍会启动本次安装的服务，只是不随下次系统启动。
6. **管理员用户名和密码**：用户名默认 `admin`，密码至少 12 字符、不回显、没有预置密码。

直接回车采用方括号内的默认值，输入无效时会提示重新填写。所有配置填写完成后才保存。安装器通过 `/dev/tty` 读取交互输入，因此支持上面的 `curl | sudo bash`，首次初始化需要真实终端。

安装完成后：

```bash
tgc
```

即可打开中文管理菜单；`tgcheckin` 仍然有效，普通用户会自动调用 sudo。安装器会创建 `/usr/local/bin/tgc` 快捷入口；如果已有其他同名文件，会保留它并提示使用 `tgcheckin`。

安装结束会根据实际配置显示访问方式。选择公网/局域网时，打开 `http://服务器IP:自定义端口`；是否能从外部访问还取决于服务器路由、防火墙和云安全组。安装器不会自动修改防火墙或配置 TLS。公网使用建议配置 HTTPS。

如果选择默认的仅本机监听，在你自己的电脑建立 SSH 隧道：

```bash
ssh -N -L 8080:127.0.0.1:8080 用户名@服务器IP
```

然后打开 **http://127.0.0.1:8080**。如果电脑的 8080 被占用，把上面第一个 `8080` 改为 `18080`，访问 `http://127.0.0.1:18080`。

如果设置了其他服务端口，将 SSH 命令中的第二个 `8080` 改为该端口。也可以运行 `tgc access` 查看按当前配置生成的访问提示。

### 已安装用户更新

从 `v1.0.0` 升级时，重新执行上面的一键部署命令，就能同时更新安装器、systemd 配置和 `tgc` 快捷入口。更新会保留原有用户名、密码、监听配置和任务；然后运行 `tgc config` 调整配置。

从 `v1.1.0` 开始，也可以直接执行 `tgc update`，它会获取最新安装器再更新。

### 用 HTTPS 域名访问

1. 配置域名 DNS，安装 Caddy，并将 [deploy/Caddyfile.example](deploy/Caddyfile.example) 中的域名替换成自己的域名。
2. Caddy 将 HTTPS 转发到 `127.0.0.1:8080`；保持应用监听本机即可。
3. 运行 `tgc config`，把「仅允许通过 HTTPS 登录」设为 `y`，其他选项按需填写或回车保留。正在运行的服务会自动重启应用配置。
4. 打开自己的 HTTPS 域名登录。

启用 Secure Cookie 后不能再通过普通 HTTP 登录。未指定浏览器访问地址时，反向代理必须保留原始 Host，并传递正确的 `X-Forwarded-Proto`；只信任来自本机的代理头。默认 Caddy 反向代理满足这一配置。也可用 `tgc domain https://checkin.example.com` 显式指定浏览器访问地址。

### Cloudflare Origin Rules / 域名提示「请求来源不匹配」

旧版直接比较浏览器的 Origin 与后端识别到的地址。Cloudflare 回源的协议、Host 或端口与浏览器访问地址不同，就可能被拒绝；单独打开 HTTPS Cookie 不能解决来源不匹配。

从 `v1.1.2` 开始，可指定唯一的浏览器访问地址：

```bash
tgc update
tgc domain https://checkin.example.com
tgc access
```

将示例替换为浏览器地址栏里的协议、域名和非默认端口；不要填回源 IP、回源端口或路径。例如浏览器使用默认 HTTPS 端口、Origin Rules 回源到服务器 `9090`，这里仍填写 `https://checkin.example.com`，不加 `:9090`。配置保存后会重启正在运行的服务，刷新网页即可重新登录。

该配置保留严格来源校验和 CSRF 验证，不信任任意代理头，也不需要把 Cloudflare 所有出口 IP 设为可信代理。设置后，其他域名及直接 IP 地址的浏览器写入请求会被拒绝。运行 `tgc domain off` 可恢复默认的同源判断。旧配置无需迁移，更新时会保留现有数据和设置。

域名配置只用于来源校验，不修改 DNS、监听地址、监听端口或 TLS。Cloudflare 直接回源时，服务须监听可达的服务器地址，Origin Rules 的目标端口须对应实际端口。Cloudflare 与源站之间的 HTTPS 需要源站 TLS 终止代理；应用自身提供 HTTP。也可使用 Cloudflare Tunnel。不要仅为消除此提示而关闭来源校验。

参考：[Cloudflare Origin Rules 设置](https://developers.cloudflare.com/rules/origin-rules/features/)、[Cloudflare Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/)。

## 设置签到

1. 在 [my.telegram.org/apps](https://my.telegram.org/apps) 申请自己的 **API ID / API Hash**。
2. 打开「Telegram 帐号 → 连接帐号」，填写备注、API ID、API Hash 和带国家代码的手机号。
3. 输入 Telegram 官方服务对话或短信发来的验证码。如提示两步验证，再输入 Telegram 两步验证密码。登录流程 5 分钟过期。
4. 在「签到任务 → 新建任务」中勾选一个或多个帐号，再填写目标对话。支持 `@机器人用户名`、公开用户名、对话 ID。也可以指定一个帐号，点击「选择对话」加载它最近 150 个已加入的对话。
5. 填写消息，例如 `/checkin`、`/sign` 或 `签到`。默认每天北京时间 09:00 执行。
6. 按需开启「失败后重试」，填写发送超时与最多重试次数；按钮文字、成功回复文本及等待回复超时独立设置。先手动执行一次，并在 Telegram 和「执行记录」核实。

可以连接多个帐号，并为每个帐号添加多个任务。重新登录同一个 Telegram 帐号会更新现有会话，保留它的任务。

### 多帐号任务集合

在新建任务中勾选多个帐号，填写一份消息、目标对话、计划和回复校验，再点击「创建集合」。**任务列表只显示一行集合**，例如「每日签到 · 3 个帐号」，侧栏和已启用任务统计也按一项计数。点击帐号数量可展开成员，收起后恢复一行。

- 集合支持统一编辑、启停、手动执行和删除；编辑时增减勾选帐号即可调整成员。保留成员的任务 ID 和执行历史不变，移除成员或删除集合也会保留历史执行记录。
- 集合成员与独立任务共用一个发送队列，按入队顺序发送。收到 Telegram 的消息发送确认后，间隔 2 秒开始下一条任务；回复在后台独立等待，不占用发送队列。发送最终失败后同样间隔 2 秒继续。手动执行和定时执行使用相同规则，每个帐号分别记录结果。
- 某一成员失败不会中止后续成员。执行集合时，暂停、限流冷却或已在队列中的成员不会重复加入；轮到执行时会再次检查帐号、任务、全局开关与冷却状态。排队时间不占发送或等待回复超时。
- 所有选中的帐号都必须能访问同一个目标。对话列表只来自你指定的一个帐号，不代表其他帐号已加入该群或可以访问该私聊。
- 单次最多选择 100 个帐号，支持全选和清空。创建或编辑整体保存，失败时撤销本次全部更改，避免只保存一部分成员。
- 只选一个帐号时新建普通任务。已有普通任务保持原样；集合即使删减到一个成员，仍保留为集合。

批量创建不会立即发送签到消息，启用后按下一次计划执行；也可点击「执行集合」手动验证。

### 计划格式

采用 APScheduler 的五段 Cron：**分 时 日 月 星期**。

| Cron | 计划 |
|---|---|
| `0 9 * * *` | 每天 09:00 |
| `30 8,20 * * *` | 每天 08:30 和 20:30 |
| `0 9 * * mon-fri` | 工作日 09:00 |
| `0 */6 * * *` | 每 6 小时的整点 |

**星期数字是 0=周一、6=周日，与传统 Unix Cron 的星期数字不同。建议使用 `mon`–`sun`。** 每个任务单独设置 IANA 时区，例如 `Asia/Shanghai`、`UTC`。夏令时地区可能有不存在或重复的墙上时间，建议将重要任务安排在非切换时段或使用 UTC。

### 按钮与成功校验

- 按钮文字采用完全匹配。只点击普通文本按钮和回调按钮；不打开链接、不提交联系方式/位置、不执行支付或授权按钮。
- 成功回复采用文本包含匹配，区分大小写。例如填写 `签到成功`。
- 同时配置按钮与成功文本时，点击按钮后收到的消息/编辑事件才能确认成功。
- 群组中只处理**回复本次命令或相关按钮消息**的结果，忽略其他人的消息。只在群里广播消息、却不引用本次命令的机器人可能显示「等待回复超时」。
- 同一帐号可在前一任务等待回复时发送下一任务，共用连接接收回复。如果连续任务发往同一会话，且前次的回复期限尚未结束，只接受明确引用对应命令或相关按钮消息的回复；未引用的回复无法可靠归属，不会用来同时确认多个任务。超时说明会提示此情况，可在 Telegram 核实或错开该会话的任务计划。
- 回调弹窗提示本身不计入回复校验；如果机器人只在弹窗中报告结果，可以不填成功文本，并人工核实。
- 不支持验证码识别、自动加入群组、多步交互脚本、WebApp 签到或复杂人机验证。程序不会绕过 Telegram 或机器人限制。

### 失败后重试

在新建或编辑任务 / 集合时勾选「失败后重试」。默认关闭，升级不会自动为旧任务开启。

- **发送超时**：5–300 秒，默认 30 秒，每次尝试独立计时，包含连接、解析目标对话及提交消息；不包含等待回复的时间。
- **最多重试次数**：1–10 次，默认 2 次，不包含首次发送。例如「30 秒、2 次」最多尝试 3 次，每次发送阶段超过 30 秒或遇到临时错误后，间隔 2 秒重试。Telegram 客户端处理临时服务错误时也可能有短暂等待。
- 只重试网络 / 连接错误、超时和 Telegram 临时服务错误（例如 `RpcCallFailError`）。无发送权限、会话失效及限流等错误直接停止；`FloodWait` 继续保存帐号冷却时间。
- **收到消息发送确认后不再进入发送重试**。等待回复超时、没有匹配成功文本、按钮处理失败均不重发签到消息，原有「等待回复超时」配置独立生效。
- 执行记录注明尝试和重试次数。连接阶段始终未提交消息时显示「失败」；提交后无法确定是否送达时显示「结果未知」，需要在 Telegram 核实。`RpcCallFailError` 表示 Telegram 未正常完成 RPC，不等于消息一定没有发出。
- 同一次执行复用 Telegram 的 `random_id` 做消息去重；缺少消息 ID 或返回重复标识时停止重试并报告「结果未知」。这是 [Telegram 发送接口](https://core.telegram.org/method/messages.sendMessage) 提供的去重机制，不保证跨服务重启的恰好一次投递。手动再次执行或下一次定时执行属于新消息。

关闭此选项时只尝试一次，发送阶段最多等待 90 秒。服务中断后不会恢复旧重试，也不会补发旧消息。集合配置统一应用于每个成员，成员分别计算自己的重试次数。

### 结果与调度行为

| 状态 | 含义 |
|---|---|
| 已发送 | 消息已提交，或按钮已点击；未配置成功文本校验 |
| 已发送，等待回复确认 | Telegram 已确认消息发送，正在独立等待按钮 / 成功文本 |
| 已确认 | 收到包含配置成功文本的回复 |
| 等待回复超时 | 消息已发送，但在期限内未收到可匹配的按钮 / 成功回复；不会重发 |
| 未确认 | 消息已发送，但回复处理出错或服务中断 |
| 排队中 | 等待统一发送队列，尚未开始发送 |
| 发送中 | 正在连接、解析目标、提交消息或进行允许的发送重试 |
| 失败 | 例如 Telegram 明确返回 FloodWait |
| 结果未知 | 网络错误、中断或服务停止，可能已经发出消息，需要核实 |
| 已跳过 | 排队时任务/帐号/全局被暂停，或帐号在冷却中 |

所有初始签到消息统一排队发送，回复确认可同时进行。相同计划时间的独立任务和集合成员按任务 ID 排队；不同计划时间按到期时间排序，手动执行按入队顺序追加。排队时间不计入发送超时与回复超时。发送重试仍占用当前发送位置，成功或停止重试后才轮到下一条。同一任务手动执行至少间隔 30 秒，且排队、发送或等待回复期间都不能重复提交。

例如 A 的消息于 09:00:01 被确认发送，即使 A 配置了 120 秒的回复等待，B 也会在 09:00:03 左右开始发送。A 会先显示「已发送，等待回复确认」，匹配回复后变为「已确认」，或到期后变为「等待回复超时」。这些状态每 2 秒自动刷新。

遇到 Telegram `FloodWait` 会保存帐号冷却截止时间，不占着任务一直等待，也不会立即重试。服务重启后仍保留冷却状态。

调度提前保存下一次时间，提交消息期间中断且无法确定是否送达时记为「结果未知」，**不自动重发**。停机后最多执行 60 秒以内的迟到计划，超过此窗口直接推进到下一次；暂停期间也不积压补发。已发出的消息不能通过暂停撤回。系统不承诺跨网络故障的“恰好一次”投递。

服务停止或重启时，尚未执行的排队任务标为「已跳过」；已发送但仍在等待回复的任务标为「未确认」，保留消息已发送的事实。发送准备阶段的失败说明会区分「连接 Telegram」「检查帐号会话」「查找目标对话」等阶段；若同时写着「消息未发送」，表示还没有提交消息请求。

## 命令行快捷指令

```bash
tgc                        # 中文交互菜单
tgc help                   # 列出快捷指令，无需提权
tgc start                  # 启动服务
tgc stop                   # 停止服务
tgc restart                # 重启服务
tgc service-status         # systemd 状态
tgc status                 # 应用配置摘要
tgc config                 # 完整配置向导（setup 等效）
tgc public 9090            # 监听所有 IPv4 网卡，端口 9090
tgc local 8080             # 仅监听本机，端口 8080
tgc public                 # 切换到公网/局域网，保留当前端口
tgc bind ::1 8080          # 指定 IPv6 地址和端口
tgc port 8088              # 只修改端口
tgc domain https://checkin.example.com  # 设置浏览器访问地址，适配 Cloudflare/反向代理
tgc domain off             # 恢复默认的同源判断
tgc username myadmin       # 修改用户名，清除所有 Web 登录
tgc passwd                 # 交互修改密码，清除所有 Web 登录
tgc enable                 # 开启开机自启
tgc disable                # 关闭开机自启，不停止当前服务
tgc pause                  # 暂停全部后续签到
tgc resume                 # 恢复签到调度
tgc list-jobs              # 查看任务与帐号 ID
tgc toggle-job 1 off        # 关闭任务 1；on 为开启
tgc access                 # 显示当前监听和访问方式
tgc logs                   # 查看服务日志
tgc backup                 # 一致性数据库备份
tgc update                 # 备份后更新，启动失败自动回滚代码
```

`tgc` 与 `tgcheckin` 完全等效。菜单既支持数字选择，也支持直接输入指令，例如 `port 9090`、`public 9090`、`restart`；输入 `q` 或 `0` 退出。

`config/setup/public/local/bind/port/domain` 保存后会应用开机自启配置，并重启**正在运行**的服务；若服务已停止，则保持停止，稍后用 `tgc start` 启动。更改绑定地址或端口后，需要使用新地址访问 Web。`enable/disable` 只改变开机自启状态，`pause/resume` 只改变签到调度，不停止 Web。

`config` 向导修改已有配置时，回车保留当前值；新密码留空也会保留原密码。`passwd` 和 `username` 可单独修改凭据，无需重启服务。密码只通过不回显的交互提示输入，不支持明文密码命令行参数。

任务详细配置通过 Web 编辑，也可参照 [example.jobs.json](example.jobs.json) 批量新增：

```bash
sudo tgcheckin import-jobs /tmp/my-jobs.json
```

文件须能被 `tgcheckin` 系统用户读取。导入是新增操作，不覆盖原任务。示例默认关闭，避免误发送。目标帐号必须已在 Web 登录。

## 数据、备份与恢复

| 位置 | 用途 |
|---|---|
| `/opt/tgcheckin/releases/` | 独立版本及 Python 虚拟环境 |
| `/opt/tgcheckin/current` | 当前版本符号链接 |
| `/var/lib/tgcheckin/` | 配置、数据库、加密主密钥 |
| `/var/backups/tgcheckin/` | 手动及更新前备份 |
| `/etc/systemd/system/tgcheckin.service` | systemd 服务 |

密码用 Argon2 哈希；手机号、API 凭据、Telegram StringSession 用 Fernet 加密；两步验证密码只用于当前请求，不写入数据库。Web 使用 HttpOnly / SameSite Cookie、CSRF 校验、来源校验和登录限速，登录会话 12 小时到期。API 不返回完整手机号、API Hash 或 Telegram 会话。

**`master.key` 和数据库必须一起备份。** 密钥存放在同一服务器，因此这主要防止单独泄露数据库时暴露会话，无法抵御已取得服务器权限的攻击者。备份包含密钥和登录数据，应视为敏感文件保存，不可提交到 Git。

恢复已知可信的备份：

```bash
sudo tgcheckin stop
sudo tgcheckin backup
sudo install -d -m 700 /var/lib/tgcheckin-restore
sudo tar -xzf /var/backups/tgcheckin/你的备份.tar.gz -C /var/lib/tgcheckin-restore
# 移走旧数据库及其 WAL/SHM，避免与恢复库混用；保留为本地回滚副本
sudo mv /var/lib/tgcheckin /var/lib/tgcheckin-before-restore
sudo mv /var/lib/tgcheckin-restore /var/lib/tgcheckin
sudo chown -R tgcheckin:tgcheckin /var/lib/tgcheckin
sudo chmod 700 /var/lib/tgcheckin
sudo find /var/lib/tgcheckin -type f -exec chmod 600 {} \;
sudo tgcheckin reset-password
sudo tgcheckin start
```

若之前已存在 `tgcheckin-before-restore` 目录，先为旧副本改一个不同名字。重置密码会清除备份中的旧 Web 登录会话。

删除帐号会移除其本地会话和任务，但不会注销 Telegram 设备。如需撤销服务器登录，前往 Telegram「设置 → 设备」结束对应会话。

## 连接帐号失败时

旧版在发送验证码时可能显示 `请求未完成（ValueError）`。一个已复现的原因是：Telegram 要求切换数据中心，客户端完成切换后却没有剩余重试次数。`v1.1.1` 已为登录流程启用有限重试；签到消息默认不重试，从 `v1.3.0` 起可在任务中单独开启发送失败重试。

执行 `tgc update` 更新后，刷新网页并重新获取验证码。新版会显示失败阶段、错误类型和诊断编号，运行 `tgc logs` 可以查找对应记录。诊断日志不记录手机号、API Hash、验证码、密码或会话内容。

- `ApiIdInvalidError`：核对 `my.telegram.org/apps` 中同一个应用的 API ID 和 API Hash。
- `PhoneCodeInvalidError` / `PhoneCodeExpiredError`：输入本次网页请求收到的验证码；过期后重新获取。
- `FloodWaitError` 或手机号请求过于频繁：按提示等待，避免连续点击发送。
- 连接阶段超时或网络错误：检查部署服务器到 Telegram 的网络连接；网页可以访问不代表服务器能够连接 Telegram。

看到其他错误时，提供失败阶段、错误类型和诊断编号即可，无需公开任何凭据。

## 本地运行与开发

```bash
git clone https://github.com/Jk-z-Box/telegram-checkin-manager.git
cd telegram-checkin-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m app.cli init
python -m app.cli serve
```

默认数据目录为当前目录的 `data/`。可通过 `TGCHECKIN_DATA=/绝对路径` 改写。`python -m app.cli init` 同样使用配置向导；配置子命令也可通过 `python -m app.cli port 9090` 等方式使用，但手动运行时需要自行重启进程。只有 Linux 安装器创建的 systemd 服务配置了绑定低端口的能力，普通用户手动运行建议使用 1024 以上端口。

只运行一个应用进程；不要使用多个 Uvicorn workers，目录锁会拒绝第二个服务进程。

测试：

```bash
pip install -r requirements-dev.txt
bash scripts/check.sh
```

测试使用模拟 Telegram 客户端或传输层，覆盖认证、CSRF、加密、任务管理、调度、限流、备份与按钮处理。登录回归测试通过真实 Telethon 请求处理验证数据中心切换与有限重试，并覆盖两步验证和错误脱敏。配置测试还覆盖安装向导、无效输入重填、保留旧密码、凭据修改后会话失效、快捷指令与服务状态保持；Shell 指令测试模拟 systemctl/runuser，不操作开发机服务。自动化测试不连接 Telegram。Linux 安装脚本仍需要在实际 systemd 主机做安装验证；本地 macOS 开发测试不等于已通过 Linux 部署验收。

[deploy/github-actions-ci.example.yml](deploy/github-actions-ci.example.yml) 提供可选 CI 示例。拥有工作流写入权限后，将它放到 `.github/workflows/ci.yml` 即可启用；仓库默认不包含已启用的 Actions 工作流。

## 技术与参考

Python / FastAPI / Telethon / APScheduler / SQLite。界面无需 Node 构建、外部字体或 CDN，固定依赖版本见 `requirements.txt`。

- [Telegram API 申请](https://core.telegram.org/api/obtaining_api_id)
- [Telethon Session 文档](https://docs.telethon.dev/en/stable/concepts/sessions.html)
- [Telethon 客户端接口](https://docs.telethon.dev/en/stable/modules/client.html)
- [APScheduler Cron 与时区](https://apscheduler.readthedocs.io/en/stable/modules/triggers/cron.html)
- [FastAPI 反向代理](https://fastapi.tiangolo.com/advanced/behind-a-proxy/)

请只操作自己的帐号和允许自动签到的对话，遵守对应机器人及 Telegram 的使用规则。MIT License。
