# 申请 macOS 开发者签名

GhostMic 可以免费发布临时签名的预览版，但 macOS 可能在首次打开时阻止运行。为了提供经过 Apple 认证的下载版，可使用 **Developer ID Application** 证书签名并提交 Apple 公证。

## 1. 加入 Apple Developer Program

1. 准备开启双重认证的 Apple 账户，并使用真实法定姓名。Apple 可能要求进行身份验证。
2. 在 [Apple Developer Program 注册页面](https://developer.apple.com/programs/enroll/)或 Apple Developer App 中，选择以个人或组织身份加入。
3. 按提示完成验证和付款。Apple 当前公布的标准年费为 **99 美元/会员年**；实际价格和付款方式以注册时所在地区显示为准。

个人开发者可以使用个人身份；以公司或其他组织身份注册时，Apple 通常要求组织是合法实体并提供 D‑U‑N‑S 编号。

## 2. 创建 Developer ID Application 证书

会员生效后，打开 [Certificates, Identifiers & Profiles](https://developer.apple.com/account/resources/certificates/list) 的 **Certificates**，点击添加按钮，选择 **Developer ID → Developer ID Application**。签应用时选择 Application；Installer 证书是签 `.pkg` 安装器用的。

按 Apple 的[证书申请说明](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)创建证书签名请求（CSR）。常见做法是在将来用于签名的 Mac 上打开「钥匙串访问」，选择「证书助理 → 从证书颁发机构请求证书」，将请求存到磁盘后上传。下载生成的 `.cer`，在同一台 Mac 上打开并导入钥匙串。

证书申请会创建私钥；私钥应留在钥匙串里，不需要把 Apple 账户密码或私钥发给别人。完成后可在终端确认系统是否识别到签名身份：

```bash
security find-identity -v -p codesigning
```

## 3. 签名和公证

证书就绪后，应用及其内嵌程序需要按 Apple 的要求签名，再提交公证并装订结果。公证需要单独配置 Apple 账户或 API 凭据；将凭据保存在本机钥匙串或安全的构建环境，不要写入仓库。具体步骤和正式发布流程应在证书就绪后针对实际安装包验证。

参考：[Apple Developer Program 会员内容及价格](https://developer.apple.com/programs/whats-included/)、[Apple Developer Program 注册要求](https://developer.apple.com/support/enrollment/)、[Developer ID 证书](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)。
