# Pull Request

> 复制到 PR 描述里填写。没有实际跑过的验证不要勾；没验证的写在“未验证项”。

## 1. 变更范围

- 这个 PR 解决什么问题：
- 改了哪些模块/文件：
- 明确不做什么（避免范围蔓延）：

关联 Issue：#

## 2. 风险与影响

- [ ] 影响失败/成功判定（异常、超时、pending、观察失败、锁冲突）
- [ ] 影响实验口径（任务集/指标/重复次数/判分标准）
- [ ] 影响设备锁或并发
- [ ] 影响证据归档（`runs/`、`results.jsonl`、`task_log.json`）
- [ ] 影响隐私/截图上传/Web 鉴权
- [ ] 只改文档/注释/测试

说明：

## 3. 验证

### 已执行

```text
# 命令 + 实际输出（粘贴关键行）
pytest -q
ruff check .
python experiments/run_experiment.py --tasks experiments/tasks.json --dry-run
```

- CI：
- 真机：未跑 / 已跑（设备型号、任务、证据路径）
- 真实 API：未跑 / 已跑（provider、模型、任务）
- 失败路径验证：列出实际构造/观察到的失败场景

### 未验证项

- [ ] 真机点击/输入/发送
- [ ] 跨分辨率/跨 ROM
- [ ] 真实模型 API 稳定性
- [ ] 多应用真实规划输出
- [ ] baseline 联调
- 其他：

## 4. 实验口径影响

- 是否改动 `tasks.json` / `metrics.py` / `test.md` / 判分标准：是 / 否
- 若是：关联的 experiment Issue、两人确认记录、旧口径 → 新口径、是否需要整批重跑
- 指标数值是否受到影响：

## 5. 回滚

- 回滚方式（revert / 开关 / 配置）：
- 回滚后需要重新跑什么：

## 6. Checklist

- [ ] 一个 PR 一个主题，没有夹带无关格式化
- [ ] 没有提交 `.env`、key、权重、`runs/`、`experiments/results/`、真实截图
- [ ] 新增/修改的行为有测试，失败路径也被覆盖
- [ ] 没有用 `importorskip` 让关键测试静默跳过
- [ ] README/文档与代码一致；不一致处已改
- [ ] 实验口径变更已走 Issue 并记录
- [ ] 合并方式为 Squash；commit message 符合 Conventional Commits
