# coding=UTF-8
"""
Lustra: gender tier 1 for posts without usable tags (docs/plans/gender-filter-model.md §3, option A0).
A pretrained anime image tagger (WD tagger v3, ViT, SmilingWolf, Apache-2.0) runs on this computer: its predicted danbooru
tags go through the same tag rules as tier 0 (lustra/gender.py, 'db' ruleset), so a missing '1girl' / '1boy' / 'no_humans'
is recovered from the picture. Its minor coded tags (loli, shota, child...) are an extra safety gate (§4.2).
Two backends:
- a model server (lustra/model_server.py, HTTP API): the app and the CLI only send the picture and get the predicted tags back,
  so they need neither numpy / onnxruntime nor the model (the desktop app bundle is built without them). Address:
  -gender_model_url / LUSTRA_GENDER_MODEL_URL, optional token: -gender_model_token / LUSTRA_GENDER_MODEL_TOKEN;
- in process (no address given): needs numpy + onnxruntime + Pillow (pip install "lustra[model]") and the model files.
The model runs on a GPU when onnxruntime has a GPU provider (onnxruntime-gpu: CUDA; ROCm, DirectML) and the CPU otherwise
(LUSTRA_GENDER_MODEL_DEVICE: auto, cpu, gpu). lustra/model_hw.py tells which, the app warns before turning the model on.
Images are fetched by the downloader like thumbnails and never stored; the model server decodes them in memory. Only the
verdict (label, confidence, evidence) is cached per post. The model is a download of ~380 MB (pinned revision, sha256 checked).

  python -m lustra.gender_model status | install | remove | classify FILE...
"""
#########################################
#
#

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .gender import GenderConf, GenderLabel, GenderResult, infer_from_tags, rules_for_module
from .model_hw import GPU_PROVIDERS, hardware_check
from .safety import blocked_tag

__all__ = ('MODEL_ID', 'BadImage', 'GenderTagger', 'ModelUnavailable', 'combine', 'install_model', 'model_status', 'remove_model')

MODEL_ID = 'wd-vit-tagger-v3'
MODEL_REPO = 'SmilingWolf/wd-vit-tagger-v3'
MODEL_REVISION = '7f6b584d0bd3f55c4531f14ba3d4761b2bccdc0f'
MODEL_LICENSE = 'Apache-2.0'
MODEL_FILES = {  # name: (size, sha256) of the pinned revision
    'model.onnx': (378536310, '35f23693620b668f4d53fd3c62bf65e40af739bc52c7eb0fbc49258b58d065b6'),
    'selected_tags.csv': (308468, '298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217'),
}
MODEL_URL = 'https://huggingface.co/{repo}/resolve/{rev}/{name}'
INPUT_SIZE = 448
GENERAL_THRESHOLD = 0.35
'''tags kept as model evidence (the threshold SmilingWolf suggests for general tags)'''
SAFETY_THRESHOLD = 0.15
'''minor coded tag at this probability or above: the post is blocked (low on purpose, plan §4.2)'''
REAL_THRESHOLD = 0.5
'''photo_(medium) / realistic: the model is meant for drawings and 3D renders, a photo stays 'unknown' (plan §4.3)'''
GENDER_TAGS_PREFIX_MAX = 64
ENV_MODEL_DIR = 'LUSTRA_GENDER_MODEL_DIR'
'''tests / developers / model server: a folder holding model.onnx + selected_tags.csv'''
ENV_MODEL_URL = 'LUSTRA_GENDER_MODEL_URL'
'''address of a model server (python -m lustra.model_server): the model runs there'''
ENV_MODEL_TOKEN = 'LUSTRA_GENDER_MODEL_TOKEN'
'''token of the model server (client and server side)'''
ENV_MODEL_DEVICE = 'LUSTRA_GENDER_MODEL_DEVICE'
'''where the model runs: auto (a GPU when onnxruntime has a GPU provider, else the CPU), cpu, gpu (fails without one)'''
DEVICES = ('auto', 'cpu', 'gpu')
API_DEFAULT_URL = 'http://127.0.0.1:8765'
API_VERSION = 'v1'
API_TIMEOUT = 60
API_LAN_SUFFIXES = ('.local', '.lan', '.home.arpa', '.internal')


class ModelUnavailable(Exception):
    pass


class BadImage(ValueError):
    """this picture only: not an image, or one Pillow cannot decode (the model itself is fine)"""


def model_dir() -> pathlib.Path:
    if env := os.environ.get(ENV_MODEL_DIR):
        return pathlib.Path(env)
    from .extensions import data_dir
    return data_dir() / 'models' / MODEL_ID


def runtime_available() -> str:
    """'' when numpy + onnxruntime + Pillow can be imported, else the missing package"""
    for mod in ('numpy', 'onnxruntime', 'PIL'):
        try:
            __import__(mod)
        except ImportError:
            return {'PIL': 'pillow'}.get(mod, mod)
    return ''


def model_status() -> dict:
    d = model_dir()
    files = {name: (d / name).is_file() and (d / name).stat().st_size == size for name, (size, _) in MODEL_FILES.items()}
    return {'id': MODEL_ID, 'repo': MODEL_REPO, 'revision': MODEL_REVISION, 'license': MODEL_LICENSE, 'path': str(d),
            'installed': all(files.values()), 'bytes': sum(s for s, _ in MODEL_FILES.values()), 'missingRuntime': runtime_available(),
            'source': f'https://huggingface.co/{MODEL_REPO}'}


def install_model(progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None) -> dict:
    """Downloads the pinned model files (size + sha256 checked, written to .part then renamed). Already complete files are kept"""
    import requests

    from .vcs.version import APP_VERSION
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    total = sum(s for s, _ in MODEL_FILES.values())
    done = 0
    for name, (size, sha) in MODEL_FILES.items():
        dest = d / name
        if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == sha:
            done += size
            continue
        part = dest.with_name(f'{name}.part')
        h = hashlib.sha256()
        got = 0
        url = MODEL_URL.format(repo=MODEL_REPO, rev=MODEL_REVISION, name=name)
        with requests.get(url, stream=True, timeout=60, headers={'User-Agent': f'Lustra/{APP_VERSION}'}) as r:
            r.raise_for_status()
            with part.open('wb') as f:
                for chunk in r.iter_content(1 << 20):
                    if cancel is not None and cancel.is_set():
                        f.close()
                        part.unlink(missing_ok=True)
                        raise ModelUnavailable('cancelled')
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(done + got, total)
        if got != size or h.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise ModelUnavailable(f'{name}: size / checksum mismatch (got {got} bytes)')
        part.replace(dest)
        done += size
    (d / 'manifest.json').write_text(json.dumps({'id': MODEL_ID, 'repo': MODEL_REPO, 'revision': MODEL_REVISION, 'license': MODEL_LICENSE,
                                                 'installedAt': int(time.time())}, indent=1), encoding='utf-8')
    GenderTagger.reset()
    return model_status()


def remove_model() -> dict:
    GenderTagger.reset()
    d = model_dir()
    for name in (*MODEL_FILES, 'manifest.json', *(f'{n}.part' for n in MODEL_FILES)):
        (d / name).unlink(missing_ok=True)
    return model_status()


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


@dataclass(slots=True)
class ModelVerdict:
    result: GenderResult
    safety_tag: str = ''
    real: bool = False


class GenderTagger:
    """One onnxruntime session per process, thread-safe (onnxruntime runs concurrent calls)"""
    model_id = MODEL_ID
    _instance: GenderTagger | None = None
    _lock = threading.Lock()

    def __init__(self, folder: pathlib.Path, device: str = 'auto') -> None:
        if device not in DEVICES:
            raise ModelUnavailable(f'device must be one of {", ".join(DEVICES)}: {device!r}')
        if missing := runtime_available():
            raise ModelUnavailable(f'python package \'{missing}\' is missing: pip install "lustra[model]"')
        if not all((folder / n).is_file() for n in MODEL_FILES):
            raise ModelUnavailable('the gender model is not installed here: python -m lustra.gender_model install (~380 MB), '
                                   'or use a model server (-gender_model_url, python -m lustra.model_server)')
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        opts.log_severity_level = 3  # errors only: CUDA adds harmless 'Memcpy nodes' warnings on every load
        names = dict((prov, dev) for prov, dev, _ in GPU_PROVIDERS)
        gpu = [] if device == 'cpu' else [p for p in ort.get_available_providers() if p in names][:1]
        if device == 'gpu' and not gpu:
            raise ModelUnavailable('no GPU runtime: onnxruntime here is a CPU build (pip install "onnxruntime-gpu[cuda,cudnn]")')
        if gpu == ['CUDAExecutionProvider'] and hasattr(ort, 'preload_dlls'):
            try:  # onnxruntime-gpu[cuda,cudnn]: CUDA and cuDNN come from the nvidia-* pip packages, not the system
                ort.preload_dlls()
            except Exception:
                pass
        self.session = ort.InferenceSession(str(folder / 'model.onnx'), sess_options=opts, providers=[*gpu, 'CPUExecutionProvider'])
        self.device = names.get(self.session.get_providers()[0], 'cpu')  # a GPU provider that failed to load leaves the CPU
        if device == 'gpu' and self.device == 'cpu':
            raise ModelUnavailable(f'{gpu[0]} did not load (CUDA / cuDNN libraries, driver): the model would run on the CPU')
        self.input = self.session.get_inputs()[0].name
        with (folder / 'selected_tags.csv').open(encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        self.names = [r['name'] for r in rows]
        self.general = [i for i, r in enumerate(rows) if r['category'] == '0']

    @classmethod
    def get(cls, device: str = '') -> GenderTagger:
        """the model of this process; device: auto / cpu / gpu ('' = LUSTRA_GENDER_MODEL_DEVICE, else auto)"""
        with cls._lock:
            if cls._instance is None:
                device = device or os.environ.get(ENV_MODEL_DEVICE, '').strip().lower() or 'auto'
                cls._instance = GenderTagger(model_dir(), device)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    @staticmethod
    def preprocess(image: bytes):
        """WD tagger input: RGB on white, padded to a square, 448 px, BGR float32 0-255, NHWC"""
        import numpy as np
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression bombs: an error above twice this size
        with Image.open(io.BytesIO(image)) as im:
            im.draft('RGB', (INPUT_SIZE * 2, INPUT_SIZE * 2))  # jpeg: decode at a reduced size, faster
            im = im.convert('RGBA')
            canvas = Image.new('RGBA', im.size, (255, 255, 255, 255))
            canvas.alpha_composite(im)
            rgb = canvas.convert('RGB')
        side = max(rgb.size)
        square = Image.new('RGB', (side, side), (255, 255, 255))
        square.paste(rgb, ((side - rgb.width) // 2, (side - rgb.height) // 2))
        square = square.resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)
        arr = np.asarray(square, dtype=np.float32)[:, :, ::-1]
        return np.ascontiguousarray(arr)[None, ...]

    def predict(self, image: bytes) -> dict[str, float]:
        """general tags with their probability (>= 0.05). BadImage: this picture only; other errors come from the model"""
        try:
            x = self.preprocess(image)
        except Exception as err:  # not a picture, truncated, decompression bomb
            raise BadImage(f'{type(err).__name__}: {err}'[:200]) from None
        out = self.session.run(None, {self.input: x})[0][0]
        return {self.names[i]: float(out[i]) for i in self.general if out[i] >= 0.05}


def classify(probs: dict[str, float], *, video: bool = False) -> ModelVerdict:
    """Predicted tags -> label through the tier 0 rules ('db': the tagger vocabulary is danbooru's).
    Confidence: the weakest probability among the tags the label rests on (>= 0.85 high, >= 0.6 medium), a video thumbnail
    is one frame of the video: at most medium (plan §3.5)"""
    safety = next((t for t, p in sorted(probs.items(), key=lambda x: -x[1]) if p >= SAFETY_THRESHOLD and blocked_tag([t])), '')
    real = max(probs.get('photo_(medium)', 0.0), probs.get('realistic', 0.0) if probs.get('photo_(medium)', 0.0) > 0.2 else 0.0) >= REAL_THRESHOLD
    tags = [t for t, p in probs.items() if p >= GENERAL_THRESHOLD]
    res = infer_from_tags(tags, rules_for_module('db'))
    if real or res.label == GenderLabel.UNKNOWN or res.conf is None:
        return ModelVerdict(GenderResult(GenderLabel.UNKNOWN, None, res.flags, (), False, 'model'), safety, real)
    weakest = min((probs.get(t, 0.0) for t in res.evidence), default=0.0)
    conf = GenderConf.HIGH if weakest >= 0.85 else GenderConf.MEDIUM if weakest >= 0.6 else GenderConf.LOW
    conf = min(conf, res.conf)
    if video:
        conf = min(conf, GenderConf.MEDIUM)
    evidence = tuple(f'{t}~{probs.get(t, 0.0):.2f}' for t in res.evidence)[:8]
    return ModelVerdict(GenderResult(res.label, conf, res.flags, evidence, res.conflict, 'model'), safety, False)


def combine(tag_res: GenderResult, model: GenderResult, min_conf: GenderConf) -> GenderResult:
    """Tier 0 + tier 1 (plan §3.4): tags confident enough win without the model; a weaker tag label must be confirmed by
    the model (conservative: disagreement -> unknown); a trap is never turned into a female by the model"""
    if tag_res.label != GenderLabel.UNKNOWN and tag_res.conf is not None and tag_res.conf >= min_conf:
        return tag_res
    if 'trap' in tag_res.flags and model.label == GenderLabel.ONLY_FEMALE:
        return GenderResult(GenderLabel.UNKNOWN, None, tag_res.flags, tag_res.evidence, True, 'tags+model')
    if tag_res.label != GenderLabel.UNKNOWN:
        if model.label == tag_res.label:
            conf = max(tag_res.conf or GenderConf.LOW, model.conf or GenderConf.LOW)
            return GenderResult(tag_res.label, conf, tag_res.flags | model.flags, tag_res.evidence + model.evidence, False, 'tags+model')
        return GenderResult(GenderLabel.UNKNOWN, None, tag_res.flags, tag_res.evidence, True, 'tags+model')
    if model.label != GenderLabel.UNKNOWN:
        return GenderResult(model.label, model.conf, tag_res.flags | model.flags, model.evidence, model.conflict, 'model')
    return tag_res


def needs_model(tag_res: GenderResult, min_conf: GenderConf) -> bool:
    """tier 0 is not confident enough and the post is not a photo of real people (plan §3.1)"""
    if 'real' in tag_res.flags:
        return False
    return tag_res.label == GenderLabel.UNKNOWN or tag_res.conf is None or tag_res.conf < min_conf


# ---------------- verdict cache: a post's picture does not change ----------------

class VerdictCache:
    def __init__(self, path: pathlib.Path | None = None) -> None:
        if path is None:
            from .extensions import data_dir
            path = data_dir() / 'cache' / 'gender_model.sqlite'
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, model TEXT, label TEXT, conf INTEGER, '
                               'flags TEXT, evidence TEXT, safety TEXT, real INTEGER, at REAL)')
        return self._conn

    def get(self, key: str, model: str = MODEL_ID) -> ModelVerdict | None:
        with self._lock:
            row = self._db().execute('SELECT label, conf, flags, evidence, safety, real FROM verdicts WHERE key = ? AND model = ?',
                                     (key, model)).fetchone()
        if row is None:
            return None
        label, conf, flags, evidence, safety, real = row
        res = GenderResult(GenderLabel(label), GenderConf(conf) if conf else None, frozenset(json.loads(flags)), tuple(json.loads(evidence)),
                           False, 'model')
        return ModelVerdict(res, safety or '', bool(real))

    def put(self, key: str, v: ModelVerdict, model: str = MODEL_ID) -> None:
        r = v.result
        with self._lock:
            self._db().execute('INSERT OR REPLACE INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (key, model, str(r.label), int(r.conf) if r.conf else 0, json.dumps(sorted(r.flags)),
                                json.dumps(list(r.evidence)), v.safety_tag, int(v.real), time.time()))
            self._db().commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------- model server client (lustra/model_server.py) ----------------

def _is_loopback(host: str) -> bool:
    import ipaddress
    host = host.strip('[]').lower()
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_network(host: str) -> bool:
    import ipaddress
    host = host.strip('[]').lower()
    if _is_loopback(host) or host.endswith(API_LAN_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def check_api_url(url: str) -> str:
    """Normalized model server address (scheme://host[:port][/path], no trailing slash). Plain http only to this computer or
    a local network: the pictures and the token would travel in clear otherwise. Raises ValueError"""
    from urllib import parse as url_parse
    url = str(url or '').strip()
    if len(url) > 300:
        raise ValueError('model server address is too long')
    u = url_parse.urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError(f'model server address must look like http://host:port: {url!r}')
    if u.username is not None or u.password is not None or u.query or u.fragment:
        raise ValueError('model server address: no user name, password, query or fragment')
    _ = u.port  # ValueError on a bad port
    if u.scheme == 'http' and not _is_local_network(u.hostname):
        raise ValueError(f'model server address: {u.hostname} is not on this computer or the local network, use https')
    return f'{u.scheme}://{u.netloc}{u.path.rstrip("/")}'


def api_is_this_computer(url: str) -> bool:
    from urllib import parse as url_parse
    return _is_loopback(url_parse.urlsplit(url).hostname or '')


class ModelApi:
    """Client of a model server: sends a picture, gets the predicted tags back (same shape as GenderTagger.predict)"""
    TAGS_MAX = 4000

    def __init__(self, url: str, token: str = '') -> None:
        import requests
        try:
            self.url = check_api_url(url)
        except ValueError as err:
            raise ModelUnavailable(str(err)) from None
        self.model_id = MODEL_ID
        self.info: dict = {}
        self._session = requests.Session()
        self._session.trust_env = not api_is_this_computer(self.url)  # a system proxy must not catch calls to this computer
        self._session.headers['User-Agent'] = 'Lustra gender-model client'
        if token:
            self._session.headers['Authorization'] = f'Bearer {token}'

    def _request(self, method: str, path: str, **kwargs):
        import requests
        for attempt in range(3):
            try:
                r = self._session.request(method, f'{self.url}/{API_VERSION}/{path}', allow_redirects=False, **kwargs)
            except requests.RequestException as err:
                raise ModelUnavailable(f'model server {self.url} is not reachable ({type(err).__name__})') from None
            if r.status_code == 503 and attempt < 2 and self._busy(r):  # every worker of the server is running the model
                time.sleep(1 + attempt)
                continue
            return r
        return r

    INFO_KEYS = ('id', 'repo', 'revision', 'license', 'inputSize', 'server', 'workers', 'loaded', 'device', 'idleUnload',
                 'deviceSetting')

    @staticmethod
    def _busy(r) -> bool:
        """503 'busy' is worth a retry; a model that cannot be loaded is not"""
        try:
            return r.json().get('error') == 'busy'
        except Exception:
            return True

    @staticmethod
    def _fail(r) -> None:
        if r.status_code == 401:
            raise ModelUnavailable('model server: wrong or missing token')
        try:
            why = str(r.json().get('error') or '')[:200]
        except Exception:
            why = ''
        raise ModelUnavailable(f'model server: HTTP {r.status_code}{f": {why}" if why else ""}')

    def status(self, timeout: float = 5) -> dict:
        t0 = time.monotonic()
        r = self._request('GET', 'status', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        try:
            info = r.json()
            assert isinstance(info, dict) and isinstance(info.get('id'), str)
        except Exception:
            raise ModelUnavailable(f'{self.url} is not a Lustra model server') from None
        self.model_id = info['id'][:64]
        self.info = {k: info[k] for k in self.INFO_KEYS if isinstance(info.get(k), str | int | bool)}
        if isinstance(info.get('hardware'), dict):
            self.info['hardware'] = info['hardware']
        self.info['ms'] = int((time.monotonic() - t0) * 1000)
        return self.info

    def load(self, timeout: float = 180) -> dict:
        """loads the model now (the server would load it on the first picture), so the first job does not wait"""
        r = self._request('POST', 'load', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        return self.status()

    def unload(self, timeout: float = 10) -> dict:
        """frees the memory (RAM / VRAM) the model holds on the server; the next picture loads it again"""
        r = self._request('POST', 'unload', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        return self.status()

    def predict(self, image: bytes) -> dict[str, float]:
        r = self._request('POST', 'tag', data=image, headers={'Content-Type': 'application/octet-stream'}, timeout=API_TIMEOUT)
        if r.status_code in (400, 413, 415):  # this picture only: not an image, too large
            return {}
        if r.status_code != 200:
            self._fail(r)
        try:
            tags = r.json()['tags']
            assert isinstance(tags, dict) and len(tags) <= self.TAGS_MAX
            return {str(t)[:128]: min(max(float(p), 0.0), 1.0) for t, p in tags.items() if isinstance(p, int | float)}
        except Exception:
            raise ModelUnavailable('model server: malformed answer') from None


_api_clients: dict[tuple[str, str], ModelApi] = {}
_api_lock = threading.Lock()


def backend(api_url: str = '', token: str = ''):
    """The model a process uses: the model server at api_url (or LUSTRA_GENDER_MODEL_URL), else the model in process.
    Raises ModelUnavailable (server not reachable, wrong token, model or runtime missing)"""
    url = api_url or os.environ.get(ENV_MODEL_URL, '')
    if not url:
        return GenderTagger.get()
    token = token or os.environ.get(ENV_MODEL_TOKEN, '')
    try:
        key = (check_api_url(url), token)
    except ValueError as err:
        raise ModelUnavailable(str(err)) from None
    with _api_lock:
        if (client := _api_clients.get(key)) is None:
            client = ModelApi(*key)
            client.status()  # fails early, learns the model id (verdict cache key)
            _api_clients[key] = client
        return client


def api_status(url: str, token: str = '', action: str = '') -> dict:
    """desktop app: can the model server at url be used, and on what hardware (check: lustra/model_hw.py hardware_check).
    action 'load' / 'unload': load the model now / free its memory, then the status"""
    out: dict = {'url': str(url or ''), 'reachable': False, 'local': False, 'error': ''}
    try:
        out['url'] = check_api_url(url)
        out['local'] = api_is_this_computer(out['url'])
        api = ModelApi(out['url'], token)
        out['model'] = api.status()
        out['reachable'] = True
        if action in ('load', 'unload'):
            out['model'] = getattr(api, action)()
    except (ValueError, ModelUnavailable) as err:
        out['error'] = str(err)
    if m := out.get('model'):
        out['check'] = hardware_check(m.get('hardware'), str(m.get('device') or '') if m.get('loaded') else '',
                                      str(m.get('deviceSetting') or ''))
    return out


def classify_image(image: bytes, *, video: bool = False) -> ModelVerdict:
    return classify(GenderTagger.get().predict(image), video=video)


# ---------------- posts of a downloader (download pipeline and preview) ----------------

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
IMAGE_MAX_BYTES = 12 * 1024 * 1024
MODEL_WORKERS = 3
_cache: VerdictCache | None = None
_cache_lock = threading.Lock()


def _shared_cache() -> VerdictCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = VerdictCache()
        return _cache


def image_url_for(dwn, raw: str) -> str:
    """picture the model looks at: the sample (~850 px, much better than a 180 px thumbnail) when it is an image, else the
    thumbnail (videos: their poster frame)"""
    from urllib import parse as url_parse
    h = dwn._local_addr_from_string(str(raw))
    i = h.find(' sample_url="')
    sample = h[i + 13:h.find('"', i + 13)] if i >= 0 else ''
    if sample and url_parse.urlparse(sample).path.lower().endswith(IMAGE_EXTS):
        url = sample
    else:
        url = dwn.extract_preview_url(h)
    if url.startswith('//'):
        url = f'https:{url}'
    elif url.startswith('/'):
        url = f'{dwn._get_sitename().rstrip("/")}{url}'
    return url if url.startswith(('https://', 'http://')) else ''


def _fetch_image(dwn, url: str) -> bytes:
    r = dwn.wrap_request(url, max(1, min(int(getattr(dwn, 'retries', 3) or 3), 3)), 'GET')
    if r is None or not str(r.headers.get('content-type', '')).lower().startswith('image/'):
        return b''
    data = r.content
    return data if len(data) <= IMAGE_MAX_BYTES else b''


def verdicts_for(dwn, posts: list[tuple[str, str]], *, workers: int = MODEL_WORKERS) -> dict[str, ModelVerdict]:
    """{post id: verdict} for (post id, raw item) pairs. Cached verdicts are reused; posts whose picture cannot be fetched
    are absent (they stay 'unknown'). Raises ModelUnavailable when the model server cannot be used, or the model or its
    runtime is missing (in process)"""
    from concurrent.futures import ThreadPoolExecutor
    tagger = backend(getattr(dwn, 'gender_model_url', ''), getattr(dwn, 'gender_model_token', ''))
    cache = _shared_cache()
    abbr = dwn._get_module_abbr()

    def one(post_id: str, raw: str) -> tuple[str, ModelVerdict | None]:
        key = f'{abbr}:{post_id}'
        if (hit := cache.get(key, tagger.model_id)) is not None:
            return post_id, hit
        url = image_url_for(dwn, raw)
        data = _fetch_image(dwn, url) if url else b''
        if not data:
            return post_id, None
        try:
            probs = tagger.predict(data)
        except ModelUnavailable:
            raise
        except Exception:  # undecodable picture: no verdict
            return post_id, None
        if not probs:  # the model server refused this picture
            return post_id, None
        v = classify(probs, video=dwn._media_kind(raw) == 'video')
        cache.put(key, v, tagger.model_id)
        return post_id, v

    out: dict[str, ModelVerdict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix='gender-model') as ex:
        for post_id, v in ex.map(lambda p: one(*p), posts):
            if v is not None:
                out[post_id] = v
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else 'status'
    if cmd == 'status':
        url = os.environ.get(ENV_MODEL_URL, '')
        print(json.dumps(api_status(url, os.environ.get(ENV_MODEL_TOKEN, '')) if url else model_status(), indent=1))
        return 0
    if cmd == 'install':
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if time.monotonic() - last[0] > 2 or done == total:
                last[0] = time.monotonic()
                print(f'{done / 1048576:.0f} / {total / 1048576:.0f} MB', file=sys.stderr)
        print(json.dumps(install_model(progress), indent=1))
        return 0
    if cmd == 'remove':
        print(json.dumps(remove_model(), indent=1))
        return 0
    if cmd == 'classify':
        tagger = backend()  # LUSTRA_GENDER_MODEL_URL: through the model server
        for f in args[1:]:
            v = classify(tagger.predict(pathlib.Path(f).read_bytes()), video=f.lower().endswith(('.mp4', '.webm')))
            print(json.dumps({'file': f, **v.result.as_dict(), 'safety': v.safety_tag, 'real': v.real}, ensure_ascii=False))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
