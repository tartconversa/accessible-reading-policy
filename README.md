# 多格式阅读支持的适用证据与服务方案

这里保存本项目需要的合成资料、资料检查工具与适用性分析应用，所有人员、学校、机构、价格和业务记录均为演练数据。

资料契约见 [资料契约.md](资料契约.md)，机读字段说明位于 `materials/contract.json`，原始记录位于 `materials/data.json`。**原始资料在整个应用中只读**，应用产生的状态与导出只写入 `outputs/`（已在 `.gitignore` 中忽略）。

## 应用解决什么问题

有声听读、可调字号文本与普通纸本对不同学生是否有帮助，不能用三组平均完成率直接回答：格式供给受设备与授权限制、设备故障与主动退出含义不同、同一学生按不同顺序交叉体验多种格式。应用把体验安排、实际使用、理解表现、退出原因与设备授权连接起来，输出：

1. **证据分析**：仅用随机交叉臂的同生配对效果（家庭聚类加权自助区间、配对 t 检验、顺序/残留检验），偏好选择臂只作描述；未完成不计零分，设备故障不插补，不适退出进入边界敏感性分析。
2. **方案规划**：在终端并发、格式授权、授权期限、网络时段、教师工时与个人必要可访问条件下枚举格式组合，比较预期受益与未覆盖风险；必要保障优先，证据不足群体保留小批量继续观察，试用资源不挤占已确认的必要服务。
3. **交付与缺口**：把服务请求拆成按请求提供、供给驱动切换、授权拒绝三类，拒绝与切换保留原请求，后续估计不做成功者分析。
4. **应急重估**：授权减少或设备故障后重算可兑现性，标出受影响的持续承诺与剩余可观察内容；故障切换不得被重新标注为学生偏好。
5. **可复核报告**：每个服务点回答“为什么采用这组格式、证据适用于谁、承诺给谁却未能提供、缺口如何影响下一轮、供给冲击影响什么”，方法与限制随结果保留。

## 运行环境

Python 3.11.2，仅使用标准库，无第三方依赖。Linux 使用 `python3`；Windows 可使用 `python`。

## 可重复运行的入口

端到端演练（读取资料 → 证据 → 组合与方案 → 承诺核对 → 交付缺口 → 应急重估 → 报告 → 演示录入一条交付记录）：

```sh
python3 run_demo.py
# 安全隔离模式（剥离当前目录与环境变量，防 planted 模块）：
python3 -I run_demo.py
```

固定自助种子（默认 `20260401`），同一份资料重复运行结果逐字节一致。输出：

- `outputs/report.md`：完整中文报告（方法卡、限制、六个业务问题的服务点回答）
- `outputs/analysis.json`：机读结果，含原始资料 sha256 指纹
- `outputs/delivery_records.json`：负责人录入的实际交付/拒绝派生记录

命令行子命令（同样支持 `-I`）：

```sh
python3 run_demo.py evidence --output outputs/evidence.json
python3 run_demo.py plan --bundle paper,audio --output outputs/plan.json
python3 run_demo.py gaps
python3 run_demo.py contingency
python3 run_demo.py record-delivery --record 一条交付记录.json --output-dir outputs
python3 run_demo.py report --output-dir outputs
```

`--bundle` 取 `paper`、`large_text`、`audio` 的逗号组合；缺省为全部三种。交付记录 JSON 必须含 `id/person_id/service_id/on/requested/provided/reason`，实际提供与请求不一致时原因字段必填，编号重复会被拒绝。

## 业务测试

```sh
python3 -m unittest discover -s tests -v
python3 -I -m unittest discover -s tests -v   # 隔离模式同样通过
```

- `tests/test_materials.py`：资料读取、结构错误识别与原始业务事实（原有）。
- `tests/test_evidence_stats.py`：未完成不计零分、设备故障不插补、随机臂/偏好臂隔离、手工复算配对差值、家庭聚类自助可复现、分布函数参考值、敏感性与支持缺口。
- `tests/test_planning_delivery_cycle.py`：必要格式不被平均效果取消、组合排序与授权约束、观察名额上限、承诺缺口审计、请求三来源、成功者偏差诊断、V1 供给冲击绑定承诺、来源守卫、端到端报告与可重复性。

## 资料结构检查

```sh
python3 inspect_materials.py
```

该工具只核对编号、关联关系和字段并显示行数与资料指纹，不产生业务分析结论。
