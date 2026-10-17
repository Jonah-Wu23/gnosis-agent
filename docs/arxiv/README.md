# arXiv 投稿文件 / arXiv submission files

**When Evaluation Prevents Learning: Functional Accumulation in Self-Evolving Agents**

Zonghe Wu · University College Dublin & Chang'an University

`gnosis.pdf` 为完整论文，`gnosis-arxiv.tar.gz` 为上传源码包。正文入口为 `gnosis.tex`，附录为 `appendix.tex`。

`gnosis.pdf` is the complete paper. `gnosis-arxiv.tar.gz` is the source bundle for upload. The main source is `gnosis.tex`; the appendix is `appendix.tex`.

正文直接读取 `gnosis.bbl`。`references.bib` 保留可编辑文献库，修改文献后需同步生成 `gnosis.bbl`。四幅图片与 ECAI 模板依赖均位于当前目录，上传包包含全部编译依赖。

The manuscript reads `gnosis.bbl` directly. `references.bib` preserves the editable bibliography; bibliography edits require regenerating `gnosis.bbl`. All four figures and the ECAI template dependencies are in this directory. The upload bundle contains every compilation dependency.

在当前目录编译两次，输出到 `.build/gnosis.pdf`：

Compile twice from this directory. The output is `.build/gnosis.pdf`:

```bash
mkdir -p .build
pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build gnosis.tex
pdflatex -interaction=nonstopmode -halt-on-error -output-directory=.build gnosis.tex
```

研究代码与分析入口见[仓库说明](../../README.md)。

See the [repository README](../../README.md) for research code and analysis commands.
