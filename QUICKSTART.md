# CodePrint quickstart

```bash
./install.sh            # venv + dependencies + `codeprint` launcher in ~/.local/bin
./setup.sh              # create folders/database/config, self-test rendering + PDF
codeprint doctor        # which languages are available?

codeprint generate samples/hello.py --title "Experiment 01"     # runs it, saves screenshot #1
codeprint page create "Experiment 01"                           # page 1
codeprint page add 1 --page 1
codeprint page preview 1                                        # A4 preview + utilization
codeprint page export 1 --output experiment01.pdf               # print-ready A4 PDF
```

If `codeprint` is not found, add `~/.local/bin` to your PATH (the installer never edits your shell config).
