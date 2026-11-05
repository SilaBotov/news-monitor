name: news-monitor

on:
  schedule:
    # UTC. Это 9:00, 12:00, 15:00, 18:00, 21:00 по Москве
    - cron: "0 6,9,12,15,18 * * *"
  workflow_dispatch:
    inputs:
      mode:
        description: "post - опубликовать пост, check - только проверить ленты"
        type: choice
        options: [post, check]
        default: post
      rubric:
        description: "Рубрика для ручного запуска (пусто = по времени)"
        type: choice
        options: ["", service, humor]
        default: ""

permissions:
  contents: write

concurrency:
  group: news-monitor
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: news-monitor/requirements.txt

      - run: pip install -r news-monitor/requirements.txt

      - name: Run
        env:
          # Если у видео-монитора секреты называются иначе, поменяй имена после "secrets."
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GROQ_API_KEY_NEWS: ${{ secrets.GROQ_API_KEY_NEWS }}
          RUBRIC: ${{ inputs.rubric }}
        run: |
          if [ "${{ inputs.mode }}" = "check" ]; then
            python news-monitor/news_monitor.py --check
          else
            python news-monitor/news_monitor.py
          fi

      - name: Commit state
        if: ${{ inputs.mode != 'check' }}
        run: |
          git config user.name "news-monitor-bot"
          git config user.email "news-monitor-bot@users.noreply.github.com"
          git add news-monitor/state.json
          if ! git diff --cached --quiet; then
            git commit -m "news-monitor: update state"
            git pull --rebase
            git push
          fi
