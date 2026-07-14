"""Thin wrapper around the X API v2 endpoints this app needs.

Deliberately narrow: only the calls the sync engine actually uses,
with clear errors on rate limits / auth / scope problems instead of any
scraping fallback.
"""
import httpx

from backend import x_auth

API_BASE = "https://api.twitter.com/2"

TWEET_FIELDS = "created_at,author_id,conversation_id,entities,attachments,referenced_tweets"
MEDIA_FIELDS = "url,preview_image_url,type"
USER_FIELDS = "username,name"
BOOKMARKS_EXPANSIONS = "author_id,attachments.media_keys"


class XApiError(Exception):
    pass


class XClient:
    def __init__(self):
        self.http = httpx.Client(timeout=30.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {x_auth.get_valid_access_token()}"}

    def _get(self, path: str, params: dict) -> dict:
        resp = self.http.get(f"{API_BASE}{path}", params=params, headers=self._headers())
        if resp.status_code == 429:
            reset = resp.headers.get("x-rate-limit-reset", "unknown")
            raise XApiError(
                f"X API rate limit hit on {path}. Resets at unix time {reset}. "
                "Not retrying automatically — re-run sync after that."
            )
        if resp.status_code == 401:
            raise XApiError(f"X API returned 401 Unauthorized on {path}. Your token may be invalid — try /auth/login again.")
        if resp.status_code == 403:
            raise XApiError(
                f"X API returned 403 Forbidden on {path}: {resp.text}. "
                "Check that your X app has the bookmark.read scope approved."
            )
        if resp.status_code >= 400:
            raise XApiError(f"X API returned {resp.status_code} on {path}: {resp.text}")
        return resp.json()

    def get_me(self) -> dict:
        data = self._get("/users/me", {})
        return data["data"]

    def get_bookmarks(self, user_id: str, pagination_token: str | None = None) -> dict:
        params = {
            "max_results": 100,
            "tweet.fields": TWEET_FIELDS,
            "expansions": BOOKMARKS_EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
            "user.fields": USER_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return self._get(f"/users/{user_id}/bookmarks", params)

    def get_tweet(self, tweet_id: str) -> dict | None:
        data = self._get(f"/tweets/{tweet_id}", {"tweet.fields": TWEET_FIELDS})
        return data.get("data")

    def expand_thread(self, tweet: dict) -> list[dict] | None:
        """Walk backward through replied_to references, while the author stays
        the same, to reconstruct a self-authored thread up to and including
        the bookmarked tweet. Returns None if the tweet isn't part of one.

        Note: this only recovers the thread *up to* the bookmarked tweet, not
        later replies the author posted after it — X API v2's Basic access
        tier has no reverse "replies to this tweet" lookup; that needs the
        recent-search endpoint, which is a separate (and more restricted)
        access tier. If you hit that limitation, it'll surface as a
        thread_text that ends at the bookmarked tweet rather than the whole
        thread.
        """
        chain = [tweet]
        seen = {tweet["id"]}
        current = tweet
        while True:
            refs = current.get("referenced_tweets") or []
            replied_to_id = next((r["id"] for r in refs if r["type"] == "replied_to"), None)
            if not replied_to_id or replied_to_id in seen:
                break
            parent = self.get_tweet(replied_to_id)
            if not parent or parent.get("author_id") != tweet["author_id"]:
                break
            chain.append(parent)
            seen.add(parent["id"])
            current = parent

        if len(chain) <= 1:
            return None
        chain.reverse()
        return chain
