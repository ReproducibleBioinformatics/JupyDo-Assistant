"""The language model, started ON DEMAND: a llama.cpp server container, started through
the Docker socket when the first user opens the assistant and stopped when the last
one closes it. Several users share one instance. A session with no messages for
SESSION_IDLE_S seconds is closed (the user closed the tab without choosing).

Configuration (environment variables):
  ASSISTANT_LLM_IMAGE      the llama.cpp server image (built from our fork)
  ASSISTANT_MODEL_DIR      HOST folder that holds the model file (mounted read-only)
  ASSISTANT_MODEL_FILE     the model file in that folder
  ASSISTANT_LLM_NETWORK    Docker network shared with the hub (jupyterhub_network);
                           empty: the server port is published on 127.0.0.1 instead
                           (to run the service outside the hub, for development)
  ASSISTANT_LLM_PORT       that port on 127.0.0.1 (default 18080)
  ASSISTANT_LLM_THREADS    CPU threads of the model (default 4)
  ASSISTANT_LLM_URL        a llama.cpp server that is already running: nothing is
                           started or stopped (development)
Python 3.9 compatible.
"""
import asyncio
import json
import logging
import os
import time
import uuid

from tornado.httpclient import AsyncHTTPClient, HTTPClientError

log = logging.getLogger("assistant")

CONTAINER = "jupydo-assistant-llm"
SESSION_IDLE_S = 600
START_TIMEOUT_S = 120


class LLMError(Exception):
    pass


class LLM:
    def __init__(self):
        self.external_url = os.environ.get("ASSISTANT_LLM_URL", "").rstrip("/")
        self.image = os.environ.get("ASSISTANT_LLM_IMAGE", "ghcr.io/reproduciblebioinformatics/llama-server:latest")
        self.model_dir = os.environ.get("ASSISTANT_MODEL_DIR", "/srv/JupyDo/assistant")
        self.model_file = os.environ.get("ASSISTANT_MODEL_FILE", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self.network = os.environ.get("ASSISTANT_LLM_NETWORK", "jupyterhub_network")
        self.port = int(os.environ.get("ASSISTANT_LLM_PORT", "18080"))
        self.threads = os.environ.get("ASSISTANT_LLM_THREADS", "4")
        self.sessions = {}          # session id -> time of its last message
        self.lock = asyncio.Lock()  # one start/stop at a time
        self.http = AsyncHTTPClient()

    @property
    def url(self):
        if self.external_url:
            return self.external_url
        return f"http://{CONTAINER}:8080" if self.network else f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------ sessions
    async def open_session(self):
        """A new session; the model is started if it is not running."""
        sid = uuid.uuid4().hex
        async with self.lock:
            self.sessions[sid] = time.time()
            await self._start()
        log.info("Session %s opened (%d open)", sid[:8], len(self.sessions))
        return sid

    def touch(self, sid):
        if sid not in self.sessions:
            raise LLMError("This conversation was closed (more than 10 minutes without messages): start again.")
        self.sessions[sid] = time.time()

    async def close_session(self, sid):
        """Close the session; the model is stopped when no session is left."""
        async with self.lock:
            if self.sessions.pop(sid, None) is not None:
                log.info("Session %s closed (%d open)", sid[:8], len(self.sessions))
            if not self.sessions:
                await self._stop()

    async def close_idle(self):
        """Close the sessions with no messages for SESSION_IDLE_S (called every minute)."""
        now = time.time()
        for sid in [s for s, t in self.sessions.items() if now - t > SESSION_IDLE_S]:
            await self.close_session(sid)

    # ------------------------------------------------------------ the container
    def _docker(self):
        import docker  # the hub image has it (dockerspawner)
        return docker.from_env()

    async def _start(self):
        if self.external_url:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._run_container)
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:   # loading the model takes a few seconds
            try:
                await self.http.fetch(self.url + "/health", request_timeout=2)
                return
            except Exception:  # noqa: BLE001 - not up yet
                await asyncio.sleep(0.5)
        raise LLMError("The assistant's model did not start: ask an administrator to check the logs.")

    def _run_container(self):
        import docker
        client = self._docker()
        try:
            c = client.containers.get(CONTAINER)
            if c.status == "running":
                return
            c.remove(force=True)
        except docker.errors.NotFound:
            pass
        log.info("Starting %s (%s)", CONTAINER, self.image)
        client.containers.run(
            self.image, name=CONTAINER, detach=True, auto_remove=True,
            command=["-m", "/models/" + self.model_file, "-c", "8192", "-t", self.threads,
                     "--host", "0.0.0.0", "--port", "8080"],
            volumes={self.model_dir: {"bind": "/models", "mode": "ro"}},
            network=self.network or None,
            ports={} if self.network else {"8080/tcp": ("127.0.0.1", self.port)},
            mem_limit="3g", labels={"jupydo.service": "assistant"})

    async def _stop(self):
        if self.external_url:
            return
        def stop():
            import docker
            try:
                self._docker().containers.get(CONTAINER).stop(timeout=5)
                log.info("Stopped %s", CONTAINER)
            except docker.errors.NotFound:
                pass
        await asyncio.get_running_loop().run_in_executor(None, stop)

    # ------------------------------------------------------------ the model
    async def chat_json(self, messages, schema, temperature=0.2, max_tokens=400):
        """The model's answer to the messages, as a JSON object that follows `schema`
        (llama.cpp constrains the output to it)."""
        body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "response_format": {"type": "json_object", "schema": schema}}
        try:
            r = await self.http.fetch(self.url + "/v1/chat/completions", method="POST",
                                      headers={"Content-Type": "application/json"},
                                      body=json.dumps(body), request_timeout=120)
        except (HTTPClientError, OSError) as e:
            raise LLMError(f"The assistant's model did not answer ({e}).")
        text = json.loads(r.body)["choices"][0]["message"]["content"]
        try:
            return json.loads(text)
        except ValueError:
            raise LLMError("The model's answer was not valid JSON.")
