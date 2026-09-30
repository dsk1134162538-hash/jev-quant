# 同花顺验证码 OCR 自定义模型训练设计

日期：2026-09-30　状态：待用户确认

## 背景与问题

2026-09-30 用 ddddocr 通用模型解掉验证码阻塞后，家里三次 `live_probe.py read --broker gateway --n 10` 累计 88/90 = **97.8%** 单次查询成功率。差在 ddddocr 通用模型对同花顺这套字体的**字符级识别率只有 ~40-50%**，靠外层 8 次重试兜到 96-99%（0.55^8≈0.8%、0.6^8≈1.7%）。

失败样本模式（详见 `patches/xiadan-gateway-ocr-alnum.patch` 相关日志）：
1. **大小写只差高度的字符**（`C/c`、`K/k`、`O/o`、`P/p`、`S/s`、`U/u`、`V/v`、`W/w`、`X/x`、`Y/y`、`Z/z`——占字母表 42%）：ddddocr CRNN 结构把图片 resize 到固定高度后相对高度差衰减；训练集标签有噪声（互联网通用验证码大量是大小写不敏感的），模型学到"倾向输出小写"的偏见。
2. **形近数字/字母**（`0/O`、`5/S`、`1/l/I`、`4/A`）：字体特殊时通用模型也易混。

代价：
- 演练场景够用（97.8% × 每笔交易 2-6 次验证码，一天几笔的量级不会累出严重问题）。
- 实盘长期跑，若一天 30-50 次验证码触发，累积失败次数会拉高"读失败 3 次自动暂停"的触发概率。

## 目标

对当前同花顺客户端字体的 4 位字母数字验证码，单次识别率从 ~45% 拉到 **≥98%**（8 次外层重试就变成 ~100%）。

- **不改**：网关现有 OCR 调用路径、`_solve_captcha` 循环、`_keep_alnum` 过滤、`case_mode` 开关。
- **只换**：`ddddocr.DdddOcr()` 加载的 ONNX 模型文件——从通用模型换成 dj 账户样本训出来的自定义模型。ddddocr 官方支持自定义模型加载（`import_onnx_path` + `charsets_path`）。

## 训练数据从哪儿来

**存档池**（现成的）：`E:/xiadan-gateway/assets/captcha_archive/` 已经有 `failed_*.png`（识别失败样本）和 `captcha_*_label.png`（识别成功样本，标签在文件名里）。**问题**：failed 样本没标注真值；success 样本的标签是 ddddocr 自己给的，可能是错的（服务端通过 ≠ 大小写完全对，只有大小写不敏感的字段才通得过）。

**方案**（三选一，成本 → 准确率）：

### A. 手工标注（推荐首选，最靠谱）
- 收集：让 `live_probe.py read` 或每天演练自然产生。当前存档已有 30-100 张；再攒 2-3 天到 500+ 张。
- 标注：dj 一张一张看图、输入真值，写进文件名。半小时能标 200 张，一小时 400 张。
- 好处：真值 100% 准；模型学到的就是当前字体。
- 坏处：dj 要花一小时体力活。

### B. 服务端验证反馈自动标注（省事但慢）
- 网关跑 `read`，每次识别记录：(图片, ddddocr 输出, 服务端是否通过)。
- 通过 = 标签 = ddddocr 输出（对）；被拒 = 标签未知（丢弃）。
- 好处：全自动。
- 坏处：只能收集"通过的"，而通过样本恰好是模型已经识别对的——**样本偏见严重**，训出的模型对错的样本没帮助。**不推荐单独用**。

### C. 用更强的外部 OCR 打标（中庸）
- 用百度 / 腾讯 OCR API（每天 500-1000 次免费额度）批量标签 failed 池。
- 好处：不用 dj 手动。
- 坏处：外部 API 对同花顺字体也不一定 100% 准；数据出了本机；仓库私有的意义打折。**不推荐**。

**选 A**。

## 训练与部署

工具链：[sml2h3/dddd_trainer](https://github.com/sml2h3/dddd_trainer)（ddddocr 作者的官方训练器，产出 ONNX 直接兼容 ddddocr）。

流程：
1. 数据准备：500-1000 张 PNG，文件名 = 真值（`SmKf.png`、`4620.png`）；训练/验证 8:2 切分。
2. 训练：CPU 也行（1-2 小时），GPU 更快（20-40 分钟）。目标 valid_acc ≥ 98%。
3. 导出：`custom_ocr.onnx` + `charset.txt`（52 个字母 + 10 个数字 = 62 类）。
4. 部署：放进 `E:/xiadan-gateway/assets/custom_ocr/`；修改 `src/core/ocr.py::warmup`：
   ```python
   self._ddddocr = ddddocr.DdddOcr(
       show_ad=False,
       import_onnx_path="assets/custom_ocr/custom_ocr.onnx",
       charsets_path="assets/custom_ocr/charset.txt",
   )
   ```
5. 补丁化：改动进 `patches/xiadan-gateway-ocr-alnum.patch` 或新加 `patches/xiadan-gateway-ocr-custom-model.patch`（分开更清晰）。
6. 模型文件本身**不进 jev-quant 仓库**（几 MB 二进制），由 `scripts/live_install.py` 从固定位置（本机 `E:/xiadan-gateway/assets/custom_ocr/` 或 dj 自建的私有 gist / OSS）拉取；家里训一次，放好，install 脚本每次校验存在即可。

## 验收标准

同一套 --n 30 实测：
- 单次识别成功率 ≥ 98%（原 ~45%）。
- `read --n 30`：三接口累计失败 ≤ 1。
- `live_probe.py order-test` 全程无 OCR_FAILED 抛出。
- 平均单次查询耗时 ≤ 8s（原 13-20s，因为重试少了）。

## 时间盘点与风险

- 数据收集：3 天（跟着 10-08、10-09、10-12 演练自然攒）。
- dj 手工标注：1 小时。
- 训练 + 部署：半天（含调参）。
- **风险**：训 overfit（数据太少）→ 用验证集 early stopping，valid_acc 稳定后停。同花顺字体更新 → 训练数据自然过期，需要重训（但字体更新罕见，年级别）。

## 排期建议

- **10-08 / 10-09 演练**（每天收盘后）：把当天 archive 里的样本导出，dj 每天标 15 分钟。
- **10-13 或之后**：数据够 400+ 张就开训。10-15 前完成部署 + 实测。
- **上实盘时机**：可选。97.8% 已够用；等自定义模型上线到 99%+ 后上实盘更稳，但不阻塞。
