"on":
  schedule:
    # Runs at 16:00 UTC (~noon ET) and 22:00 UTC (~6pm ET) daily.
    # GitHub may delay scheduled runs by a few minutes during high load --
    # that's normal and not something to worry about.
    - cron: '0 16 * * *'
    - cron: '0 22 * * *'
  workflow_dispatch: {}   # lets you trigger a run manually from the Actions tab

permissions:
  contents: write

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Run daily pipeline
        working-directory: .
        run: python scripts/run_daily.py

      - name: Commit and push updated data
        run: |
          git config user.name "mlb-board-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data/
          git diff --cached --quiet && echo "No changes to commit" || git commit -m "Daily board update $(date -u +%Y-%m-%d)"
          git pull --rebase origin main
          git push
