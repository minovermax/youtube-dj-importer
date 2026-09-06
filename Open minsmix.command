#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v node >/dev/null 2>&1 && [[ -d "$HOME/.nvm/versions/node" ]]; then
  node_bins=("$HOME"/.nvm/versions/node/*/bin(NOn))
  (( ${#node_bins} )) && export PATH="${node_bins[1]}:$PATH"
fi
if [[ ! -x .venv/bin/python ]]; then
  print 'First run the setup commands in README.md.'
  read '?Press Enter to close.'
  exit 1
fi
if ! .venv/bin/python ui_server.py; then
  read '?Press Enter to close.'
fi
