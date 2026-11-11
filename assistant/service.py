#!/usr/bin/env python3
"""
JupyDo "assistant" service (JupyterHub managed service, all users).

A chat page, opened in a panel over JupyDo's spawn form ("Not sure what Docker is?
Ask our assistant"): the user describes the analysis, the assistant asks follow-up
questions and suggests 1-3 images of the catalog. "Use this image" sends the image
name to the form (postMessage), which fills "Custom Image" and closes the panel.

The service (this page + catalog search) is small and always on; the language model
(llama.cpp, assistant/llm.py) is started only while someone is using the assistant.

Configuration (environment variables, besides those of assistant/llm.py):
  ASSISTANT_CATALOG   catalog.json (default: the one of this repository)
  ASSISTANT_DEV=1     no JupyterHub: no login, listens on ASSISTANT_DEV_PORT (default
                      10108); for development only
Python 3.9 compatible.
"""
import json
import logging
import os
import sys
from urllib.parse import urlparse

from tornado import ioloop, web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from assistant.catalog import Catalog  # noqa: E402
from assistant.dialogue import Conversation  # noqa: E402
from assistant.llm import LLM, LLMError  # noqa: E402

log = logging.getLogger("assistant")
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "page.html")
DEV = os.environ.get("ASSISTANT_DEV") == "1"
CATALOG = Catalog(os.environ.get("ASSISTANT_CATALOG", os.path.join(os.path.dirname(HERE), "catalog.json")))
LLM_SERVER = LLM()
CONVERSATIONS = {}   # session id -> (user name, Conversation)

if DEV:
    class Auth:
        def get_current_user(self):
            return {"name": "dev"}
else:
    from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated as Auth


class Base(Auth, web.RequestHandler):
    def check_xsrf_cookie(self):
        pass  # JSON API: cross-origin POST with JSON is blocked by the browser

    def write_error(self, status_code, **kwargs):
        exc = kwargs.get("exc_info", (None, None))[1]
        self.finish({"error": getattr(exc, "log_message", None) or self._reason})

    def body(self):
        try:  # navigator.sendBeacon sends text/plain: the body is read as JSON anyway
            return json.loads(self.request.body or b"{}")
        except ValueError:
            raise web.HTTPError(400, "invalid JSON")

    def conversation(self, sid):
        user, conv = CONVERSATIONS.get(sid, (None, None))
        if conv is None or user != self.current_user["name"]:
            raise web.HTTPError(404, "This conversation was closed (more than 10 minutes without "
                                     "messages): start again.")
        return conv


class PageHandler(Base):
    @web.authenticated
    def get(self):
        with open(PAGE) as f:
            self.finish(f.read().replace("{{catalog_made}}", CATALOG.made)
                        .replace("{{catalog_size}}", str(len(CATALOG.images))))


class StartHandler(Base):
    @web.authenticated
    async def post(self):
        try:
            sid = await LLM_SERVER.open_session()
        except LLMError as e:
            raise web.HTTPError(503, str(e))
        CONVERSATIONS[sid] = (self.current_user["name"], Conversation(CATALOG, LLM_SERVER))
        log.info("%s opened the assistant", self.current_user["name"])
        self.finish({"session": sid})


class MessageHandler(Base):
    @web.authenticated
    async def post(self):
        body = self.body()
        sid, text = str(body.get("session", "")), str(body.get("text", "")).strip()
        conv = self.conversation(sid)
        if not text:
            raise web.HTTPError(400, "empty message")
        try:
            LLM_SERVER.touch(sid)
            reply = await conv.answer(text)
        except LLMError as e:
            raise web.HTTPError(503, str(e))
        self.finish(reply)


class CloseHandler(Base):
    @web.authenticated
    async def post(self):
        body = self.body()
        sid = str(body.get("session", ""))
        if sid in CONVERSATIONS and CONVERSATIONS[sid][0] == self.current_user["name"]:
            CONVERSATIONS.pop(sid)
            await LLM_SERVER.close_session(sid)
            if body.get("image"):
                log.info("%s chose %s", self.current_user["name"], body["image"])
        self.finish({"closed": sid})


async def close_idle():
    await LLM_SERVER.close_idle()
    for sid in [s for s in CONVERSATIONS if s not in LLM_SERVER.sessions]:
        CONVERSATIONS.pop(sid, None)


def main():
    logging.basicConfig(level=logging.INFO, format="[assistant %(levelname)s] %(message)s")
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/services/assistant/")
    j = lambda p: prefix + p  # noqa: E731
    handlers = [
        (prefix, PageHandler),
        (j("api/start"), StartHandler),
        (j("api/message"), MessageHandler),
        (j("api/close"), CloseHandler),
    ]
    if not DEV:
        handlers.append((j("oauth_callback"), HubOAuthCallbackHandler))
    app = web.Application(handlers, cookie_secret=os.urandom(32))
    if DEV:
        host, port = "127.0.0.1", int(os.environ.get("ASSISTANT_DEV_PORT", "10108"))
    else:
        url = urlparse(os.environ.get("JUPYTERHUB_SERVICE_URL", "http://127.0.0.1:10108"))
        host, port = url.hostname, url.port
    app.listen(port, host)
    ioloop.PeriodicCallback(close_idle, 60 * 1000).start()
    log.info("Listening on http://%s:%d%s (catalog of %s, %d images)%s", host, port, prefix,
             CATALOG.made, len(CATALOG.images), " - DEVELOPMENT MODE, no login" if DEV else "")
    ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
