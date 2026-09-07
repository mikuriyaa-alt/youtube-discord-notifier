#!/usr/bin/env python3
"""YouTube の新着動画を Discord チャンネルに自動投稿する。

YouTube の RSS フィードを監視し、まだ通知していない動画を Discord Webhook に流す。
依存パッケージなし（Python 3.9+ の標準ライブラリのみ）。

環境変数:
  DISCORD_WEBHOOK_URL     必須。投稿先チャンネルの Webhook URL。
  YOUTUBE_CHANNEL_ID      監視するチャンネル ID（UC で始まる文字列）。
  POST_SHORTS             "false" なら Shorts を投稿しない。既定は true。
  POST_LIVE               "true" なら配信中のライブも投稿する。既定は false
                          （配信が終わってアーカイブになった時点で通常動画として投稿される）。
  MESSAGE_TEMPLATE        通常動画の投稿文。{title} {url} {channel} を埋め込める。
  SHORTS_MESSAGE_TEMPLATE Shorts の投稿文。省略時は MESSAGE_TEMPLATE と同じ。
  SHORTS_WEBHOOK_URL      Shorts だけ別チャンネルに流したい場合に設定する。
                          省略時は通常動画と同じチャンネルに投稿する。
  STATE_FILE           通知済み動画 ID の保存先。既定は state.json。
  MAX_POSTS_PER_RUN    1 回の実行で投稿する上限。暴走時の保険。既定は 5。
  DRY_RUN              "true" なら Discord に送らず、送る内容を表示するだけ。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
WATCH_URL = "https://www.youtube.com/watch?v={}"
SHORTS_URL = "https://www.youtube.com/shorts/{}"

# YouTube はブラウザ以外の UA に対して不安定なので、実在するブラウザの UA を送る。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 通知済み ID をこの件数だけ保持する。フィードは最新 15 件しか返さないので十分な余裕。
STATE_KEEP = 200


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_template(name, default):
    """投稿文テンプレートを読む。

    YAML の書き方やシェル経由かどうかで、改行が本物の改行になる場合と
    "\\n" という2文字のまま渡ってくる場合がある。どちらでも同じ結果になるよう揃える。
    """
    return (env(name) or default).replace("\\n", "\n")


def env_bool(name, default=False):
    v = env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=20):
    """本文を文字列で返す。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトを追わずにステータスコードだけ見たい時に使う。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_status_no_redirect(url, timeout=20):
    """リダイレクトを追わずに HTTP ステータスコードを返す。取得できなければ None。"""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        with opener.open(req, timeout=timeout) as res:
            return res.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        log(f"  ! ステータス取得に失敗: {e}")
        return None


def fetch_feed(channel_id, attempts=5):
    """RSS フィードを取得する。

    YouTube のフィードは正常なチャンネルでも散発的に 404 を返すことがある
    （実測で 5 回に 2〜3 回）。ここで諦めると「新着なし」と誤判定して
    状態ファイルを壊しかねないので、必ずリトライし、全滅したら None を返す。
    """
    url = FEED_URL.format(channel_id)
    for i in range(1, attempts + 1):
        try:
            body = http_get(url)
            if "<entry>" in body or "<feed" in body:
                return body
            log(f"  フィードの中身が不正 (試行 {i}/{attempts})")
        except Exception as e:
            log(f"  フィード取得に失敗 (試行 {i}/{attempts}): {e}")
        if i < attempts:
            time.sleep(2 * i)
    return None


def unescape(s):
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


def parse_feed(xml):
    """フィードを (channel_title, [entry...]) に分解する。新しい順のまま返す。"""
    m = re.search(r"<title>(.*?)</title>", xml, re.S)
    channel_title = unescape(m.group(1).strip()) if m else "YouTube"

    entries = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", block)
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        published = re.search(r"<published>(.*?)</published>", block)
        if not vid:
            continue
        entries.append(
            {
                "id": vid.group(1).strip(),
                "title": unescape(title.group(1).strip()) if title else "(無題)",
                "published": published.group(1).strip() if published else "",
            }
        )
    return channel_title, entries


def is_short(video_id):
    """Shorts なら True、通常動画なら False、判定できなければ None。

    /shorts/<id> は Shorts なら 200 を返し、通常動画なら /watch へ 303 で飛ばす。
    """
    code = http_status_no_redirect(SHORTS_URL.format(video_id))
    if code == 200:
        return True
    if code in (301, 302, 303, 307, 308):
        return False
    return None


def is_live_now(video_id):
    """今まさに配信中なら True。判定できなければ False（=通常動画として扱う）。"""
    try:
        page = http_get(WATCH_URL.format(video_id))
    except Exception as e:
        log(f"  ! ライブ判定に失敗、通常動画として扱う: {e}")
        return False
    return '"isLive":true' in page or '"isLiveNow":true' in page


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "initialized": bool(data.get("initialized")),
            "seen": list(data.get("seen", [])),
        }
    except FileNotFoundError:
        return {"initialized": False, "seen": []}
    except Exception as e:
        # 壊れた状態ファイルで過去動画を全部投稿してしまうのが最悪なので、
        # 読めなかったら未初期化として扱い、次の実行で初期化させる。
        log(f"! 状態ファイルを読めなかった ({e})。初回実行として扱う。")
        return {"initialized": False, "seen": []}


def save_state(path, state):
    state["seen"] = state["seen"][-STATE_KEEP:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def post_to_discord(webhook, content):
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "youtube-discord-notifier"},
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                if 200 <= res.status < 300:
                    return True
                log(f"  Discord から HTTP {res.status}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 429:
                # レート制限。Discord が指示する待ち時間に従う。
                try:
                    wait = float(json.loads(body).get("retry_after", 5))
                except Exception:
                    wait = 5.0
                log(f"  レート制限。{wait:.1f} 秒待つ")
                time.sleep(min(wait + 0.5, 30))
                continue
            log(f"  Discord へのPOSTが失敗 HTTP {e.code}: {body}")
            if 400 <= e.code < 500:
                return False  # URL や本文が悪い。リトライしても直らない。
        except Exception as e:
            log(f"  Discord へのPOSTが失敗 (試行 {attempt}/3): {e}")
        time.sleep(2 * attempt)
    return False


def main():
    webhook = env("DISCORD_WEBHOOK_URL")
    channel_id = env("YOUTUBE_CHANNEL_ID", "UCoHy7BYunccawKcweDPdgtg")
    state_file = env("STATE_FILE", "state.json")
    post_shorts = env_bool("POST_SHORTS", True)
    post_live = env_bool("POST_LIVE", False)
    dry_run = env_bool("DRY_RUN", False)
    template = env_template(
        "MESSAGE_TEMPLATE",
        "＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝\n🎥 新しい動画が公開されました‼️\n＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝\n{url}",
    )
    # Shorts は通常動画より圧倒的に本数が多い。同じ文面で流すと週1本の通常動画が
    # 埋もれるので、既定では見分けのつく文面にしておく。
    shorts_template = env_template(
        "SHORTS_MESSAGE_TEMPLATE",
        "＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝\n⚡️ 新しいショートが公開されました‼️\n＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝\n{url}",
    )
    shorts_webhook = env("SHORTS_WEBHOOK_URL")
    try:
        max_posts = int(env("MAX_POSTS_PER_RUN", "5"))
    except ValueError:
        max_posts = 5

    if not webhook and not dry_run:
        log("エラー: DISCORD_WEBHOOK_URL が設定されていない。")
        return 1

    log(f"チャンネル {channel_id} のフィードを取得中…")
    xml = fetch_feed(channel_id)
    if xml is None:
        # 取得できないまま状態を書き換えると取りこぼす。何もせず正常終了する。
        log("フィードを取得できなかった。今回は何もせず終了する（次回の実行で再試行）。")
        return 0

    channel_title, entries = parse_feed(xml)
    log(f"「{channel_title}」から {len(entries)} 件の動画を取得")

    state = load_state(state_file)
    seen = set(state["seen"])

    # 初回実行では既存の動画をすべて既読にする。
    # これをやらないと、設置した瞬間に過去 15 本が Discord に流れ込む。
    if not state["initialized"]:
        state["initialized"] = True
        state["seen"] = [e["id"] for e in reversed(entries)]
        save_state(state_file, state)
        log(f"初回実行: 既存の {len(entries)} 件を既読として記録した。")
        log("次回以降、ここから先に公開された動画だけを投稿する。")
        return 0

    # 古い順に処理する。公開順どおりに Discord に並ぶ。
    new_entries = [e for e in reversed(entries) if e["id"] not in seen]
    if not new_entries:
        log("新着なし。")
        return 0

    log(f"未通知の動画が {len(new_entries)} 件")
    posted = 0

    for e in new_entries:
        vid, title = e["id"], e["title"]
        url = WATCH_URL.format(vid)
        log(f"- {vid} {title}")

        if posted >= max_posts:
            log(f"  今回の投稿上限 {max_posts} 件に達した。残りは次回の実行にまわす。")
            break

        short = is_short(vid)

        if short is None:
            # 種類が分からなかった。投稿先や文面が種類で変わる設定なら、
            # 誤った振り分けをするより次回に持ち越したほうがよい。
            if not post_shorts or shorts_webhook or shorts_template != template:
                log("  Shorts 判定ができなかった。次回の実行に持ち越す。")
                continue
            # 種類によらず同じ扱いをする設定なので、判定できなくても支障はない。
            log("  Shorts 判定ができなかったが、扱いが同じなので通常動画として投稿する。")
            short = False

        if short and not post_shorts:
            log("  Shorts のためスキップ（既読として記録）")
            state["seen"].append(vid)
            continue

        if not post_live and not short:
            if is_live_now(vid):
                # 既読にしない。配信が終わってアーカイブになったら通常動画として投稿される。
                log("  ライブ配信中のためスキップ（配信終了後に投稿される）")
                continue

        tpl = shorts_template if short else template
        target = (shorts_webhook if short and shorts_webhook else webhook)
        content = tpl.format(title=title, url=url, channel=channel_title)

        if dry_run:
            log(f"  [DRY_RUN] 種類={'Shorts' if short else '通常動画'} 送信内容:")
            for line in content.splitlines():
                log(f"    | {line}")
            state["seen"].append(vid)
            posted += 1
            continue

        if post_to_discord(target, content):
            log(f"  Discord に投稿した（{'Shorts' if short else '通常動画'}）")
            state["seen"].append(vid)
            posted += 1
            time.sleep(1)  # Webhook のレート制限に余裕を持たせる
        else:
            # 投稿に失敗したものは既読にしない。次回の実行で再試行する。
            log("  投稿に失敗。次回の実行で再試行する。")

    save_state(state_file, state)
    log(f"完了: {posted} 件を投稿した。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
