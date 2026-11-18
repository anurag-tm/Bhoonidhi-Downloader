#!/usr/bin/env bash
# One-time install for Linux / macOS (needs Miniconda or Anaconda)
set -e
cd "$(dirname "$0")"
if ! command -v conda >/dev/null 2>&1; then
  echo "conda not found. Install Miniconda first: https://docs.conda.io/en/latest/miniconda.html"
  exit 1
fi
if conda env list | grep -q "^bhoonidhi "; then
  echo "Updating environment 'bhoonidhi'..."
  conda env update -n bhoonidhi -f environment.yml --prune
else
  echo "Creating environment 'bhoonidhi' (first time takes 5-15 min)..."
  conda env create -f environment.yml
fi
# skip Streamlit's one-time email question
mkdir -p "$HOME/.streamlit"
[ -f "$HOME/.streamlit/credentials.toml" ] || printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
echo
echo "Done. Start the app with:  ./run.sh"
