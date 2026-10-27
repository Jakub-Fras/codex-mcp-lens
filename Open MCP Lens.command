#!/bin/zsh

set -u
tool_folder="${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
for codex_binary in \
  "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex" \
  "/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"; do
  if [[ -x "$codex_binary" ]]; then
    export PATH="${codex_binary:h}:$PATH"
    break
  fi
done
python_command=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python_command="$candidate"
    break
  fi
done

if [[ -z "$python_command" ]]; then
  echo "MCP Lens needs Python 3.11 or newer. Install Python, then open this launcher again."
  if [[ -t 0 ]]; then read -k 1 '?Press any key to close...'; fi
  exit 1
fi

project_folder="$HOME"
echo "MCP Lens can check the Codex settings that apply to a particular folder."
echo "This does not open files in that folder. The report can show saved Codex file edit records for it."
echo "Connection activity still covers saved Codex history across folders."
if [[ -t 0 ]]; then
  read -r 'check_folder?Check settings for a particular folder? [y/N] '
else
  check_folder=""
fi
if [[ "$check_folder" == [yY] || "$check_folder" == [yY][eE][sS] ]]; then
  if command -v osascript >/dev/null 2>&1; then
    chosen_folder="$(osascript -e 'POSIX path of (choose folder with prompt "Choose the folder whose Codex settings you want to check." default location (path to home folder))' 2>/dev/null)" || chosen_folder=""
    if [[ -n "$chosen_folder" ]]; then
      project_folder="$chosen_folder"
    else
      echo "No folder selected. Using your home folder for the settings check."
    fi
  else
    read -r 'chosen_folder?Enter the full folder path: '
    if [[ -d "$chosen_folder" ]]; then
      project_folder="$chosen_folder"
    else
      echo "That folder was not found. Using your home folder for the settings check."
    fi
  fi
else
  echo "Using your home folder for the settings check."
fi
echo "Settings context: $project_folder"

cd "$tool_folder" || exit 1
"$python_command" "$tool_folder/mcp_lens.py" report \
  --project "$project_folder" \
  --output "$tool_folder/mcp-lens-report.html" \
  --share-output "$tool_folder/mcp-lens-share.html"
result=$?
if (( result != 0 )); then
  echo "MCP Lens could not make the report. The message above explains what happened."
  if [[ -t 0 ]]; then read -k 1 '?Press any key to close...'; fi
  exit "$result"
fi

echo "Your private report is ready. The separate mcp-lens-share.html contains counts only."
echo "Keep this Terminal window open while you use the Turn off or Remove buttons."
"$python_command" "$tool_folder/mcp_lens.py" manage \
  --project "$project_folder" \
  --report "$tool_folder/mcp-lens-report.html" \
  --share-output "$tool_folder/mcp-lens-share.html"
