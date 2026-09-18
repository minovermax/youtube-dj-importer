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
print 'YouTube / SoundCloud → tagged MP3'
print 'Use music you own or have permission to download.'
print 'Default download folder: ~/Music/minsmix'
read 'destination?Download folder (Enter for default): '
destination=${destination:-$HOME/Music/minsmix}
destination=${destination/#\~/$HOME}
if .venv/bin/python youtube_import.py --root "$destination" --open; then
  print 'Done. Import the MP3 from that folder into rekordbox.'
fi
read '?Press Enter to close.'
