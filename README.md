# UDM SE 上的 Mihomo 分流

让选定设备或 LAN/VLAN 的 IPv4 TCP/UDP 流量在 UDM SE 本机分流：国内域名/IP 直连，其余走你的代理订阅。保留 UniFi 的 DHCP、DNS、VLAN 和原有防火墙。

这是 [silicondawn/udmse-mihomo](https://github.com/silicondawn/udmse-mihomo) 的维护 fork。默认先准备安装文件，**不启动代理、不接管全网、不设置开机自启**。空设备范围拒绝启用；每次试用必须有自动撤销计时器。

## 这次修复了什么

- 接管规则同时限制 LAN 入口和来源地址；单设备模式还必须匹配来源 MAC。整网接管要显式填写对应子网。
- 原有 DNS 保持在 53 端口。Mihomo 在明确的网关局域网 IP 上使用独立的 1053（避免双栈通配监听），只转发选定来源发给网关的 DNS；其他设备不能访问该监听器。
- 代理入口和带随机密钥的管理 API 仅监听本机。日志默认为 warning，不公开管理面板或 SOCKS 服务。
- 自有链、独立路由标记和路由表；发现冲突立即停止准备，不清空或覆盖 UniFi 的规则。
- 运行时保存接管范围，停止时按原范围清理；先移除转发，再停止代理，并清理对应 DNS 连接状态。
- 监测代理连通性和规则完整性。节点不可用时恢复直连，健康恢复后可在试用窗口内重新接管；若 DNS 访问限制被删除或排到放行规则后面，停止试用。
- 安装使用固定版本和 SHA-256 校验、临时目录及配置检查。失败撤销本次写入；保留旧安装，避免猜测如何迁移。
- 订阅和规则下载使用独立的直连 DNS；节点解析与代理 DNS 分离，避免空订阅时的启动循环。空代理组拒绝连接，不会静默直连冒充代理成功。
- 订阅 URL 通过隐藏输入或文件读取并正确转义；私有目录 0700、配置 0600。卸载默认保留私有文件，不修改 `/etc/resolv.conf`。

## 已验证的边界

2026-09-18，先行单设备试验在 UDM SE **UniFi OS 5.1.33 / Debian 11 / Linux 4.19 ARM64** 上验证了同一 TPROXY 方案：海外 HTTPS 和 UDP 经代理、国内网站直连、其他设备出口不变、UDP/TCP DNS 正常，以及定时撤销后恢复原网络。

**先行试验不等于本 fork 的完整部署验收。** 本 fork 的安装、范围限制、冲突拒绝、故障清理和卸载有隔离的模拟回归测试；真实 v1.19.31 引擎也已在 UDM 的独立网络命名空间中验证监听地址、空缓存订阅启动，以及解析失败时保持 REJECT（不接入家庭网络）；全网负载、重启、UniFi 重新下发配置、固件升级及中国大陆网络连通性仍须在实际环境验证。不要把此项目当作 UniFi 官方支持的扩展。

目前只处理 IPv4 TCP/UDP。发现系统存在 IPv6 默认路由时拒绝启动，避免声称已分流实际绕过的 IPv6 流量。内网、Tailscale 地址、ICMP 和路由器自身发出的流量不进入透明代理。浏览器加密 DNS、Tailscale DNS 与 ECH 也可能影响域名识别；它们不会被自动关闭。

## 准备安装

在 Mac/电脑获取完整仓库；安装器不支持只下载一个 `install.sh` 后从可变分支拼装其余文件。

```sh
git clone https://github.com/xingfanxia/udmse-mihomo.git
cd udmse-mihomo
# 将已提交的源码送到 UDM；不会带入本地订阅、scratch 或其他未提交文件。
git archive HEAD | ssh root@192.168.1.1 \
  'install -d -m 700 /data/mihomo-source; tar -xf - -C /data/mihomo-source'
ssh root@192.168.1.1
cd /data/mihomo-source
bash install.sh
```

安装器会隐藏输入订阅 URL。自动化可使用 `--subscription-file /private/path/url.txt`，不要把 URL 放在命令行。可用 `--routing-file /private/path/routing.env` 提供范围配置。需要 Linux ARM64、Python 3、systemd、iptables、conntrack、flock 等工具；不安装 Docker、不替换内核。

默认固定 **Mihomo v1.19.31**，验证官方 ARM64 压缩包 SHA-256：

```text
9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4
```

如果 `/data/mihomo` 或同名 systemd 单元已存在，安装器会拒绝继续，且不停止旧服务。旧版本需要单独迁移，不能直接覆盖运行中的网络服务。

## 单设备试用

编辑 `/data/mihomo/routing.env`，使用自己的实际 IP 和 MAC，并在 UniFi 中为设备保留 DHCP 地址。示例值不可直接照抄：

```sh
SCOPE=devices
LAN_INTERFACES=(br0)
DEVICE_IPV4='192.168.1.100'
DEVICE_MAC='aa:bb:cc:dd:ee:ff'
SOURCE_CIDRS=()
IPV6_POLICY=require-no-default
DNS_LISTEN_IPV4='192.168.1.1'  # UDM 自己持有的局域网 IPv4
```

然后在 UDM 上运行：

```sh
/data/mihomo/20-mihomo.sh start       # 15 分钟后自动撤销
/data/mihomo/20-mihomo.sh status
/data/mihomo/20-mihomo.sh stop        # 提前恢复原网络
```

`start 60` 可试用 60 分钟，支持 1–1440 分钟。已启动的试用需先停止再重新开始。主进程退出会清理本项目规则；进程崩溃后应查看原因并重新开始，不会无限重启。恢复策略是直连，不是阻断所有未走代理的流量。

## 指定 LAN/VLAN 的所有设备

只有明确需要扩大范围时才使用：

```sh
SCOPE=networks
LAN_INTERFACES=(br0 br2)
SOURCE_CIDRS=('192.168.1.0/24' '192.168.2.0/24')
DEVICE_IPV4=''
DEVICE_MAC=''
IPV6_POLICY=require-no-default
DNS_LISTEN_IPV4='192.168.1.1'  # UDM 自己持有的局域网 IPv4
```

桥接接口、网段和 DNS_LISTEN_IPV4 须来自自己的 UDM 实际配置。若网关不是 192.168.1.1，安装前通过 --routing-file 提供正确地址；安装器据此生成 DNS 监听配置。每条规则仍限制入口与来源子网，不会挂在所有 WAN/LAN 入口上。**先停止试用再编辑范围**；运行中修改配置会被拒绝，恢复仍使用之前保存的范围。

安装器不创建 `/data/on_boot.d` 钩子，也不启用 systemd 开机自启。本版本不提供永久全网接管命令；请先完成设备级试用和真实环境验收。

## 管理页与总开关

安装会准备 `mihomo-admin.service`，它独立于代理进程，只监听 UDM 本机的 `127.0.0.1:9088`。启用管理页不会接管流量：

```sh
# UDM 上
systemctl start mihomo-admin.service
# 在自己的终端读取登录口令，不要公开或放进网址：
cat /data/mihomo/admin-token
```

在 Mac 上建立 SSH 隧道，然后打开 <http://127.0.0.1:9088>：

```sh
ssh -N -L 127.0.0.1:9088:127.0.0.1:9088 root@192.168.1.1
```

页面提供：

- **代理总开关**：控制已经配置的设备范围；不能从页面扩大到其他设备或修改订阅。
- **智能分流**：国内域名/IP 直连，其余走代理。
- **全部代理**：所选设备的互联网 TCP/UDP 走代理；内网、Tailscale 和路由器自身的流量仍排除。DNS 的独立下载/节点解析路径保持原配置。
- **全部直连**：停止代理并撤销接管，包括 DNS 转发，而不是只切换引擎的 direct 模式。
- **当前范围、出口和自动恢复倒计时**：来自实时状态，不把尚未完成的操作显示为成功。

启用可选择 15 分钟、1 小时或 4 小时。正在运行时切换模式**不会延长**原有计时器。全局模式会先确认 `GLOBAL` 组选中了 `PROXY`，再接管设备，避免引擎默认的 DIRECT 选择造成误判。

登录使用单独的随机管理口令；浏览器仅在内存中保存，不写入网址、本地存储或 cookie。API 验证 Bearer、Host 和同源 Origin，不开放跨域访问，也不提供执行命令或读取任意文件的接口。服务默认不设置开机自启；SSH 隧道断开后页面不可访问，但已有自动撤销计时器仍在 UDM 上执行。

## 管理与卸载

API 在 `127.0.0.1:9090`，密钥在路由器私有配置中；通过 SSH 隧道访问，不要为了面板把监听改成全网可达。混合代理入口为 `127.0.0.1:7890`，透明代理为 `127.0.0.1:7893`。规则集是国内域名和国内 IP 两份 MRS，定期通过 HTTPS 更新。

```sh
/data/mihomo/20-mihomo.sh stop
bash /data/mihomo/uninstall.sh          # 移除服务与规则，保留私有目录
bash /data/mihomo/uninstall.sh --purge  # 明确删除本安装的全部私有文件
```

卸载会先核对安装所有权和 unit 内容；遇到其他程序接管的文件会停止，不猜测删除。保留的目录包含配置、provider 缓存及原始 unit 引用，便于之后检查或显式清除。

## 开发检查

```sh
shellcheck ./*.sh
python3 -m unittest discover -s tests -p '*_test.py' -v
# 可选：安装了 Chrome 的开发机上验证桌面/手机界面与交互
uv run --with playwright python tests/admin_browser.py
```

测试用隔离的 iptables/ip/systemd 替身，不操作开发机的网络。覆盖安装失败、订阅转义、文件权限、空范围、规则冲突、IPv6 前提、单设备/网段限制、接管中途失败、配置变更后的清理、DNS 访问限制顺序及卸载顺序。没有真实路由器的测试通过不能证明实机安装或升级完成。

可选真实引擎检查见 `tests/bootstrap_smoke.py`：只允许在独立 Linux 网络命名空间中运行，入口会拒绝宿主网络。使用从 `config.yaml` 渲染并转换成 JSON 的无真实凭据测试配置，验证订阅下载不依赖尚未加载的代理；此检查不会修改系统路由或防火墙。

订阅启动路径依据固定版本源码核对：[provider 的指定出口](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/component/resource/vehicle.go#L139)、[DIRECT 的独立解析器](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outbound/direct.go#L29)、[空组 fallback 行为](https://github.com/MetaCubeX/mihomo/blob/v1.19.31/adapter/outboundgroup/parser.go#L74-L82)。
