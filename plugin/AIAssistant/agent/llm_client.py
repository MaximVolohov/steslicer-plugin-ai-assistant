"""OpenAI-compatible Chat Completions client (tool calling, image_url content)."""
import json
import ssl
import time
from urllib.parse import urlparse

import requests

from UM.Logger import Logger

RETRY_STATUS = (408, 409, 429, 500, 502, 503, 504)


class LLMError(Exception):
    def __init__(self, message, status=None, body=None):
        Exception.__init__(self, message)
        self.message = message
        self.status = status
        self.body = body or ""


def build_ca_bundle(path):
    """certifi (2019 in this build) + the Windows trust store, so modern TLS chains validate."""
    pems = []
    try:
        import certifi
        with open(certifi.where(), "r", encoding="ascii", errors="ignore") as stream:
            pems.append(stream.read())
    except Exception:
        pass
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if enum_certificates is not None:
        for store in ("ROOT", "CA"):
            try:
                for cert, encoding, trust in enum_certificates(store):
                    if encoding != "x509_asn":
                        continue
                    if trust is True or "1.3.6.1.5.5.7.3.1" in trust:  # serverAuth
                        pems.append(ssl.DER_cert_to_PEM_cert(cert))
            except Exception:
                continue
    if not pems:
        return True
    try:
        with open(path, "w", encoding="ascii", errors="ignore") as stream:
            stream.write("\n".join(pems))
        return path
    except OSError:
        return True


def normalize_base_url(url):
    url = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
    return url


class LLMClient(object):
    def __init__(self, config, ca_bundle=True):
        self._config = config
        self._ca_bundle = ca_bundle
        self._session = requests.Session()

    def _proxies(self, url):
        """Loopback endpoints (local LLM servers) must bypass a system/VPN proxy; others use it."""
        host = (urlparse(url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return {"http": None, "https": None}
        return None

    def _verify(self):
        if not self._config.get("verify_ssl"):
            try:
                requests.packages.urllib3.disable_warnings()
            except Exception:
                pass
            return False
        return self._ca_bundle

    def _headers(self):
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                   "User-Agent": "STE-Slicer-AI-Assistant/1.0"}
        key = self._config.get("api_key")
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def _url(self, suffix):
        base = normalize_base_url(self._config.get("api_base_url"))
        if not base:
            raise LLMError("Не задан адрес LLM API. Откройте настройки ассистента.")
        return base + suffix

    @staticmethod
    def _errorText(response):
        try:
            data = response.json()
            error = data.get("error", data)
            if isinstance(error, dict):
                return error.get("message") or json.dumps(error, ensure_ascii=False)[:500]
            return str(error)[:500]
        except Exception:
            return (response.text or "")[:500]

    def chat(self, messages, tools=None, cancel_event=None):
        payload = {"model": self._config.get("model"), "messages": messages,
                   "temperature": self._config.get("temperature")}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._url("/chat/completions")
        timeout = (15, max(30, int(self._config.get("request_timeout"))))
        last_error = None
        for attempt in range(4):
            if cancel_event is not None and cancel_event.is_set():
                raise LLMError("Отменено пользователем.")
            if attempt:
                time.sleep(min(2 ** attempt, 10))
            try:
                response = self._session.post(url, data=body, headers=self._headers(), timeout=timeout,
                                              verify=self._verify(), proxies=self._proxies(url))
            except requests.exceptions.SSLError as e:
                raise LLMError("Ошибка TLS при подключении к LLM: %s. Можно отключить проверку сертификата в настройках." % e)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_error = LLMError("Нет связи с LLM API (%s): %s" % (url, e))
                Logger.log("w", "[AIAssistant] LLM request failed (attempt %d): %s", attempt + 1, e)
                continue
            if response.status_code in RETRY_STATUS:
                last_error = LLMError("LLM API вернул %d: %s" % (response.status_code, self._errorText(response)),
                                      response.status_code, response.text)
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 20))
                continue
            if response.status_code >= 400:
                raise LLMError("LLM API вернул %d: %s" % (response.status_code, self._errorText(response)),
                               response.status_code, response.text)
            try:
                data = json.loads(response.content.decode("utf-8"))
                choice = data["choices"][0]
                return choice.get("message") or {}, data.get("usage") or {}, choice.get("finish_reason")
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("Неожиданный ответ LLM API: %s" % response.text[:300], response.status_code, response.text)
        raise last_error or LLMError("LLM API недоступен.")

    def listModels(self):
        url = self._url("/models")
        try:
            response = self._session.get(url, headers=self._headers(), timeout=(10, 30), verify=self._verify(),
                                         proxies=self._proxies(url))
        except requests.exceptions.RequestException as e:
            raise LLMError("Нет связи с %s: %s" % (url, e))
        if response.status_code == 404 and not normalize_base_url(self._config.get("api_base_url")).endswith("/v1"):
            raise LLMError("Адрес %s не найден (404). Возможно, в конце адреса нужен /v1." % url, 404)
        if response.status_code >= 400:
            raise LLMError("LLM API вернул %d: %s" % (response.status_code, self._errorText(response)), response.status_code)
        try:
            data = json.loads(response.content.decode("utf-8"))
        except ValueError:
            raise LLMError("Ответ /models не JSON.")
        models = data.get("data", data if isinstance(data, list) else [])
        return sorted(str(m.get("id")) for m in models if isinstance(m, dict) and m.get("id"))
