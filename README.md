# subtitle-workflow

A Codex skill for finding and reviewing multilingual subtitles for films and TV series. It looks for subtitles that match your video release, checks timing, missing lines, and translation quality, and flags anything that still needs review while preserving the original files.

[下载 v1.4.0 安装包](https://github.com/GreyGu/subtitle-workflow/releases/download/v1.4.0/subtitle-workflow-1.4.0.zip) · [版本记录](https://github.com/GreyGu/subtitle-workflow/releases) · [反馈问题](https://github.com/GreyGu/subtitle-workflow/issues)

## 安装与第一次使用

1. 下载并解压安装包，将整个 `subtitle-workflow` 文件夹复制到用户目录下的 `.agents/skills/`。Codex 的标准用户级位置为 Windows 的 `%USERPROFILE%\.agents\skills\` 或 macOS 和 Linux 的 `~/.agents/skills/`。也可复制到项目的 `.agents/skills/`，供该项目使用。升级时先备份旧文件夹，并确保技能目录下只留一个同名版本。
2. 确认目录结构为 `.agents/skills/subtitle-workflow/SKILL.md`。在 Codex 中输入 `$subtitle-workflow` 选择技能，未出现时重启 Codex。
3. 将这段提示词里的影片路径、语言和保存目录换成自己的信息。

> 使用 $subtitle-workflow，为【视频路径】寻找简体中文字幕，原音轨是【实际语言】。先按这个片源核对候选，并抽查同步和缺句。将结果保存到【目录】，保留原件，列出仍待确认的问题。

没有本地视频时，可以检索候选和复核字幕正文，声画同步留待拿到片源后核验。需要按片段逐句复核时，请在提示词中明确说明。

## 用原创样例试跑

在解压后的 `subtitle-workflow` 目录运行。需要 Python 3.10 或更高版本，只使用 Python 自带模块。Windows 可用 `py -3`，macOS 或 Linux 上也可按本机安装情况用 `python3` 替换 `python`。

```sh
python -B -X utf8 scripts/subtitle_audit.py audit assets/examples/zh-en/target.en.srt
python -B -X utf8 scripts/subtitle_audit.py map-check --target assets/examples/zh-en/target.en.srt --reference assets/examples/zh-en/source.zh.srt --mapping assets/examples/zh-en/review-map.json --require-quality-review
```

第一条检查英文 SRT（纯文本字幕）的编码、时间轴和正文结构。第二条检查中文参考字幕与英文目标字幕的逐句对应记录，并确认审核状态已填写。样例由原创短句组成，无需影片、账号或网络。检查通过表示样例文件和审核记录满足规则，真实影片的同步与翻译质量仍要结合音轨和上下文判断。

## 功能与工具

| 任务 | 能做什么 |
| --- | --- |
| 查找与选择候选 | 让 Codex 按片名、集数、剪辑版本和音轨筛选。联网查找和下载取决于可用工具和站点访问情况。 |
| 检查 SRT | 本地检查时间顺序、正文缺漏和参考覆盖。默认只读，主动导出时写入指定的新文件。 |
| 核对音画与译文 | 按原音轨和上下文判断缺句、错译及文体。视频核验按需使用 FFmpeg/ffprobe 或播放器。 |
| 长剧集和多语言 | 用 `subtitle_progress.py` 记录每集进度，识别重复候选，并在输入匹配时复用已有结果。 |

ASS/SSA 排版字幕和 PGS/SUP 图片字幕需要对应工具处理。语言方向由实际音轨和目标语言决定。

## 测试与打包

```sh
python -B -X utf8 -m unittest discover -s scripts -p "test_*.py" -v
python -B -X utf8 scripts/package_skill.py build --output dist/subtitle-workflow-1.4.0.zip
python -B -X utf8 scripts/package_skill.py verify dist/subtitle-workflow-1.4.0.zip
```

发布包只包含清单内的文件。校验器检查文件集合、版本和 SHA-256 文件指纹。脚本参数与退出码见 [使用说明](references/script-usage.md)，技能入口见 [SKILL.md](SKILL.md)。

提交问题时，请提供技能版本、操作系统、运行命令和去除私人路径后的报错。最小复现可用自编短句。项目采用 [MIT 许可证](LICENSE)，发布素材包含项目源码、文档和原创样例。
