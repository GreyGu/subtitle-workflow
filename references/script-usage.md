# 检查器接口与数据契约（1.4.0）

需要 Python 3.10+，仅用标准库，无网络请求。以下命令在技能根目录运行；路径相对当前任务，替换为用户的实际文件。UTF-8 是默认编码，其他编码须显式指定。

## 1. SRT 结构检查

```text
python -B -X utf8 scripts/subtitle_audit.py audit assets/examples/zh-en/target.en.srt
python -B -X utf8 scripts/subtitle_audit.py audit working/movie.ru.srt --encoding cp1251 --duration-seconds 5400 --max-cps 22 --max-line-length 42 --max-lines 2 --gap-seconds 45 --json evidence/audit-new.json
```

5400 仅为参数示例；时长必须读自本次实际视频。仅支持 SRT，解码错误立即停止。报告包含文件指纹、全部解析条目、空条、错误/告警、各条阅读速度和时间范围。

默认 22 每秒字符、42 每行字符、2 行、45 秒空档只是可调整的告警阈值，不是各语言通用标准。字符按去掉常见格式标签后的 Unicode 码位粗计，计入空格/标点并将换行算为空格；不等同组合字形、视觉宽度或阅读能力。图片/复杂排版/双向文字须实际渲染检查。大空档和重叠是线索，不自动判为漏译或错误。

## 2. 规范化为新副本

```text
python -B -X utf8 scripts/subtitle_audit.py normalize working/original.ru.srt --encoding cp1251 --output working/normalized-new.ru.srt
```

输出 UTF-8 BOM（编码签名）、CRLF 换行。保持解析条目数、编号、时间、可见正文、格式标签及时间行位置设置，去除条目内多余空白行和行尾空白。先内存回读比较，再写新文件并核对字节。

遇空正文、坏时间、重复编号、倒序或解码错误即拒绝。禁止覆盖源文件或任何已有输出；不重新编号、不删广告、不恢复已丢失文字、不翻译、不调轴。所有路径显式指定；程序不会默认写进影片目录。

## 3. 新版对应表

[review-map.schema.json](../assets/review-map.schema.json) 是 JSON Schema（可机器读取的数据结构规范）；[中英完整样例](../assets/examples/zh-en/review-map.json) 可直接运行。规范允许未完成记录保存，不能用结构合法替代审核通过。

根字段：

| 字段 | 要求 |
| --- | --- |
| `schema_version` | 新表固定字符串 `1.1`；未知版本拒绝，防止误读 |
| `metadata.source_languages` | 实际源音轨语言标签数组，不重复；多语可多项 |
| `metadata.target_language` | 这一份目标字幕的语言标签 |
| `metadata.reviewer` | 真实审核者/方式；模型辅助不可冒称母语人工听校 |
| `metadata.reviewed_at` | 有效日期 `YYYY-MM-DD` |
| `metadata.reference_basis` | 原文和时间基准及限制 |
| `metadata.target_convention` | 可选，地区拼写/文体/未指定等 |
| `target_sha256`、`reference_sha256` | 与实际输入字节完全相符的64位文件指纹，可由 audit 获得 |
| `links` | 对应组，默认空数组；每组包含实际 `reference_ids`、`target_ids` 数组 |
| `excluded_reference`、`target_only` | 有依据的排除或目标独有内容；默认空数组 |

语言标签只校验形式，不联网查询语言注册表；站点下载代码需另外确认。代码还会检查日期、大小写不敏感的语言去重、ID 是否存在/唯一和文件指纹；这些跨文件条件不由 Schema 单独证明。附加根级/元数据/记录注释允许保留，但程序不会解释未知扩展字段。

每个 ID 只能分配一次，拆合句用同一组中的数组表达；不能将同一条重复分配给不同组。实际序号不等于列表位置。更新文字、编号、编码或换行后指纹会变，应重新核对受影响记录，不能盲目替换旧指纹。

每个对应组的 `review` 为 `checked`、`uncertain`、`unreviewed`（默认）。新版翻译审核还需 `quality_review`：

```json
{
  "faithfulness": {"status": "pass", "note": "否定、主体和关系已按具体原句核对；说明实际证据"},
  "clarity": {"status": "revise", "note": "代词有两个可能先行词，需要改写"},
  "style": {"status": "not_reviewed"}
}
```

三个维度定义见 [translation-quality.md](translation-quality.md)。各项状态仅允许 `pass/revise/uncertain/not_reviewed`；前三种必须有非空理由。缺项视为未审核，不默认通过。未知维度/状态或非文本理由报输入错误，防止拼写错误悄悄失效。即便 `review=checked`，任何一项不是 `pass` 仍标为待复核。

排除项必须有 `id/category/reason`：

- `excluded_reference`：`non_speech`、`credit`、`untranscribed_speech`、`source_ad`、`not_in_target_cut`、`unclassified`。未转写/待分类即使有记录仍未解决；版本排除还须实际确认。
- `target_only`：`opening_credit`、`screen_text`、`verified_audio_speech`、`source_credit`。额外目标文字也须审核；严格模式下同样要求三维记录，不能通过标成“目标独有”绕开审核。

## 4. 全片对应及三维记录检查

```text
python -B -X utf8 scripts/subtitle_audit.py map-check --target assets/examples/zh-en/target.en.srt --reference assets/examples/zh-en/source.zh.srt --mapping assets/examples/zh-en/review-map.json --require-quality-review
```

对实际任务替换三条输入路径，可加 `--tolerance-ms 250 --json evidence/map-audit-new.json`。检查两份完整字幕的结构、文件指纹和全部映射段，输出两端时间差、最大/中位偏差、遗漏 ID、未转写项及三维声明统计。偏差为目标减参考，正值代表目标更晚。250 毫秒是可调筛查阈值，不是统一合格线。

一对多/多对一仅自动比整组起止；组内各句时机须另行核查。三维统计是对应组/目标独有记录数量，不是对白句数、准确率或实际音轨覆盖率。

`--require-quality-review` 用于完整翻译审核：要求新版元数据，每个对应组及目标独有条目三维都有明确记录。缺项、待改或不确定返回待复核；填满 `pass` 只能证明记录齐全，程序无法验证理由真假。全片语义审查、语言能力和实际声音仍需真实证据。

兼容性：无 `schema_version` 的旧表仍可不带严格参数运行原有结构/映射检查；不会追认其“信达雅审核通过”。旧表若带三维记录也会检查这些记录；升级时按实际情况补齐元数据和审核，不能批量改成 `pass`。

## 5. 返回码、写入与测试

| 命令 | 0 | 2 | 3 |
| --- | --- | --- | --- |
| `audit` | 无结构错误，仍可能有告警 | 输入/结构/写入错误 | 不使用 |
| `normalize` | 新副本写入并回读一致 | 输入不合法或输出已存在等 | 不使用 |
| `map-check` | 对应及所要求的审核记录通过 | 输入/结构/指纹/元数据不合法 | 未对应、未复核、时间异常、未转写或信达雅有待处理项 |

报告含 `tool_version`。JSON 输出为 UTF-8；`--json` 和规范化输出不覆盖已有文件。数据中可能包含字幕全文和个人路径，公开报告前须另行去除个人/受保护内容。

## 6. 去重与逐集续跑

`subtitle_progress.py` 只使用 Python 标准库。它不打印报告正文或字幕内容，详细记录保存在本地 JSON。

```text
python -B -X utf8 scripts/subtitle_progress.py index working/candidates --manifest evidence/candidate-index.json
python -B -X utf8 scripts/subtitle_progress.py init evidence/progress.json --title "影片名" --season 1 --episodes 20 --languages zh-Hans,ru --release "本地视频发布名"
python -B -X utf8 scripts/subtitle_progress.py record evidence/progress.json --episode 1 --language ru --stage structure --input working/S01E01.ru.srt --report evidence/S01E01.ru.audit.json --status issues --summary "2 个结束时间超出片长"
python -B -X utf8 scripts/subtitle_progress.py check evidence/progress.json --episode 1 --language ru --stage structure --input working/S01E01.ru.srt
python -B -X utf8 scripts/subtitle_progress.py status evidence/progress.json --details
```

`index` 生成完整候选清单和重复组摘要；内容相同但文件名不同的字幕只需分析一次。`init` 拒绝覆盖已有进度文件。阶段顺序为候选、结构、对应、同步、翻译、音轨、交付。`record` 保存输入 SHA-256 和可选报告 SHA-256；若该阶段输入指纹改变，会清空本阶段及之后阶段的记录。`check` 只有在输入指纹、工具版本相同且已有结果时才返回 `reusable`。报告应存于任务 `evidence/`，不要把报告全文复制进对话。状态文件只记录进度，不能证明阶段结论正确。

若检查参数会影响结果，在 `record` 和 `check` 中都提供相同的 `--config` 字符串；参数指纹变化时，该阶段及后续阶段必须重做。`check` 还会核对报告文件是否存在及其指纹是否改变。

```text
python -B -X utf8 -m unittest discover -s scripts -p "test_*.py" -v
```

测试覆盖解析、严格解码、禁止覆盖、全量映射、指纹失效、未解决话语、中译英及逆向/多语文字、信达雅缺项/类型/理由、命令行返回码及分享包验证。修改代码后重跑，并按授权对真实输入做只读回归。测试全部通过不是实际影片已全片听校。
