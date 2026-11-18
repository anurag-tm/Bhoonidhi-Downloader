#!/usr/bin/env bash
# Start the app on Linux / macOS
cd "$(dirname "$0")"
eval "$(conda shell.bash hook)"
conda activate bhoonidhi || { echo "Run ./install.sh first."; exit 1; }
echo "Opening in your browser. Keep this terminal open; press Ctrl+C to stop."
python -m streamlit run app.py --browser.gatherUsageStats false
