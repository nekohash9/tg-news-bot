# telegram news bot

this bot parses content from it-related websites and posts cleaned summaries to a single telegram channel. it is designed to run in docker and also includes rate limiting, anti-spam protection, night mode, and daily posting limits that you can change manually

## features

-   parsing multiple sources defined in sources.yaml
-   posting to one telegram channel
-   smart daily post limit (configurable)
-   anti-spam and duplicate protection using sqlite
-   night mode (no posts during quiet hours)
-   safe markdown handling for telegram
-   docker-ready setup

## project structure

-   main.py -- main bot logic
-   sources.yaml -- list of sites to parse
-   bot_config.env -- environment variables
-   requirements.txt -- python dependencies
-   dockerfile -- docker image definition
-   docker-compose.yml -- container runner
-   state.db -- sqlite database (created automatically)

## requirements

-   docker and docker-compose (or docker compose plugin)
-   a telegram bot token
-   a telegram channel where the bot is admin

## setup

1.  create a telegram bot using botfather and copy the token
2.  create a telegram channel and add the bot as administrator
3.  get the channel id (usually starts with -100)

## configuration

### bot_config.env

    bot_token=YOURTOKEN
    channel_id=YOURID
    timezone=europe/berlin
    daily_limit=20
    night_start=23
    night_end=8

### sources.yaml

example:

    sources:
      - name: hacker news
        url: https://news.ycombinator.com/
        type: hn
      - name: lobste.rs
        url: https://lobste.rs/
        type: rss

## running on a machine

1.  install docker
2.  copy the project files to the server
3.  edit bot_config.env and sources.yaml
4.  build and run:

```{=html}
docker compose build
docker compose up -d
```
5.  check logs:

```{=html}
docker logs -f your_bot
```
## notes

-   the database state.db stores sent posts and rate limit data
-   deleting state.db resets daily limits and duplicate tracking
-   make sure your channel allows bot messages

