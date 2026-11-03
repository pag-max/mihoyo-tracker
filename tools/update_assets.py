# -*- coding: utf-8 -*-
"""米游社官方素材月度更新器（独立可执行版）

从米游社（观测枢）官方 wiki 拉取三个游戏的角色 / 武器 / 遗器图标、
星魂 / 影画 / 命座图案与套装效果，增量更新到 exe 同级的 assets_wiki/
目录，并刷新名字映射表与套装效果库。

设计要点：
  * 幂等：db/wiki_manifest.json 记录每个文件的 id/url/sha1，未变更不重复下载。
  * 只写 exe 同级 assets_wiki/（可写目录），不碰 _internal。
  * 更新完自动重建 db/名字映射.json，让新角色直接出现在角色库里。

用法（普通 Python）：
  python update_assets.py --all
  python update_assets.py --all --dry-run     # 只看不下载
  python update_assets.py --compare           # 覆盖比对
  python update_assets.py --constellations    # 只同步星魂/影画/命座
  python update_assets.py --set-effects       # 只填充套装效果

用法（计划任务 / 免交互）：
  python update_assets.py --all --quiet        # 只输出日志到 update.log
  python update_assets.py --all --exit-code   # 用退出码表示成功失败

数据源（均为米游社官方）：
  星铁  https://act-api-takumi-static.mihoyo.com/common/blackboard/sr_wiki/v1/home/content/list
        ?app_sn=sr_wiki&channel_id=18(角色) / 19(光锥) / 30(遗器)
  绝区零 / 原神
        https://act-api-takumi.mihoyo.com/hoyowiki/<app>/wapi/search_insert_entry_page
        ?menu_id=<分类id>&page_size=100&page_num=<n>
        app: zzz / genshin
        zzz      menu 43 代理人 / 45 音擎 / 46 驱动盘
        genshin  menu 25 角色 / 5 武器 / 218 圣遗物
  星魂/影画/命座（每角色各一条详情页）：
        星铁   content/info?content_id=<X> 的 rpg_new_tmp_content
               modules[].name=='角色星魂'（richText 内 6 张图 + 各级文字）
        绝区零 entry_page?entry_page_id=<X> 的「影画展示1/2/3」图
               （2113×1163 宽幅插画，保宽幅缩到 480 宽，界面横切 6 段做进度条）
               + LEVEL1-6 表
        原神   entry_page?entry_page_id=<X> 的「命之座」表
               （行 HTML 内 6 个 data-image-url 图标）
  套装效果：
        原神   entry_page 基础信息 list 的「2/4件套效果」
        绝区零 entry_page tables header==['二件套','四件套']
        星铁   content/info contents 文本段的「二件套：」「四件套：」
"""
import os
import re
import sys
import json
import html
import time
import shutil
import hashlib
import argparse
import datetime
import urllib.request
import urllib.parse
import ssl
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
# 打包后（PyInstaller frozen）资源根目录是 exe 同级，而不是 __file__ 所在目录
if getattr(sys, 'frozen', False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))
ASSETS = os.path.join(HERE, 'assets')
# 米游社官方素材独立存放，避免覆盖本地既有素材；程序按名字映射表回退查找
WIKI_ASSETS = os.path.join(HERE, 'assets_wiki')
DB_DIR = os.path.join(HERE, 'db')
MANIFEST = os.path.join(DB_DIR, 'wiki_manifest.json')
ALIAS_FILE = os.path.join(DB_DIR, '名字映射.json')
LOG_FILE = os.path.join(HERE, 'update.log')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'

# ---- 数据源配置 ----------------------------------------------------------
# 新接口（hoyowiki）：app -> (目录名, {素材类别: menu_id})
NEW_API = {
    'zzz': ('zzz', {'characters': 43, 'weapons': 45, 'relics': 46}),
    'genshin': ('genshin', {'characters': 25, 'weapons': 5, 'relics': 218}),
}
# 老接口（sr_wiki）：app -> (目录名, {素材类别: channel_id})
OLD_API = {
    'hsr': ('hsr', {'characters': 18, 'weapons': 19, 'relics': 30}),
}
# 原神遗器：一套 5 个部位，图命名为「套装名_1.png ~ 套装名_5.png」
GENSHIN_SLOTS = ['生之花', '死之羽', '时之沙', '空之杯', '理之冠']

# 素材类别 -> assets 子目录
CAT_DIR = {'characters': 'characters', 'weapons': 'weapons', 'relics': 'relics'}

# 人工指定的对齐（自动匹配搞不定的特殊情况）
MANUAL = {
    '绝区零': {
        'characters': {
            '「扳機」': '「扳机」', '「11号」': '「11号」', '席德': '「席德」',
            '奧菲丝': '奥菲丝·马格努森&「鬼火」', '猫又': '猫宫 又奈',
            '悠真': '浅羽 悠真', '柏野真斗': '狛野 真斗', '月城柳': '月城 柳',
            '浮波柚叶': '浮波 柚叶', '星徽·比利': '星徽·比利·奇德',
            '比利': '比利·奇德', '雅': '星见 雅', '本': '本·比格',
            '简': '简·杜', '照': '照',
            '露西': '露西亚娜·德·蒙特夫', '莱卡恩': '冯·莱卡恩',
            '丽娜': '丽娜', '妮可': '妮可·德玛拉', '安比': '安比·德玛拉',
        },
        'weapons': {
            '「殘響」-I型': '「残响」-Ⅰ型', '「殘響」-II型': '「残响」-Ⅱ型',
            '「殘響」-III型': '「残响」-Ⅲ型', '「電磁暴」-參式': '「电磁暴」-叁式',
            '「電磁暴」-貳式': '「电磁暴」-贰式', '「電磁暴」-壹式': '「电磁暴」-壹式',
            '德瑪拉電池II型': '德玛拉电池Ⅱ型', '防暴者VI型': '防暴者Ⅵ型',
            '居家服務員': '家政员',
        },
        'relics': {
            '原始龐克': '原始朋克', '激素龐克': '激素朋克', '震星迪斯可': '震星迪斯科',
        },
    },
    '原神': {
        'characters': {
            '空': '旅行者·风', '荧': '旅行者·草', 'Linnea': '莉奈娅',
            'Lohen': '薇斯纳', 'Odette': '奥黛塔', 'Prune': '布伦妮',
            'Vesna': '沃雅妮莎', 'Vodyanitsa': '洛恩', '艾斯科菲': '奇偶·女性',
        },
        'weapons': {'「一心传」名刀': '「一心传」名刀'},
        'relics': {},
    },
    '星铁': {
        'characters': {
            '开拓者': '开拓者•记忆', '三月七': '三月七', '仙舟三月七': '仙舟三月七',
        },
        'weapons': {}, 'relics': {},
    },
}

GAME_DIR = {'星铁': 'hsr', '原神': 'genshin', '绝区零': 'zzz'}

_LOG_LINES = []
# GUI 实时进度用：若被设置，log() 每产生一行就回调它（跨线程安全，由调用方用 Qt 信号接）
LOG_CB = None
# 覆盖比对的结构化结果（给 GUI 渲染表格用）
COMPARE_ROWS = []


def log(msg, quiet=False):
    line = str(msg)
    _LOG_LINES.append(line)
    if not quiet:
        print(line, flush=True)
    if LOG_CB is not None:
        try:
            LOG_CB(line)
        except Exception:
            pass


def flush_log(quiet=False):
    """把本次运行日志追加到 update.log"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'\n===== {stamp} =====\n')
            f.write('\n'.join(_LOG_LINES))
            f.write('\n')
    except Exception:
        pass


# ---- HTTP ----------------------------------------------------------------
def _http(url, headers=None, retries=3, timeout=45):
    h = {'User-Agent': UA}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f'请求失败 {url[:110]}: {last}')


def _json(url, headers=None):
    return json.loads(_http(url, headers).decode('utf-8'))


def new_api_json(app, path, params=None):
    url = f'https://act-api-takumi.mihoyo.com/hoyowiki/{app}/wapi/{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return _json(url, {'x-rpc-wiki_app': app, 'Referer': 'https://baike.mihoyo.com/'})


def old_api_json(app_sn, path, params=None):
    url = f'https://act-api-takumi-static.mihoyo.com/common/blackboard/{app_sn}/v1/{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return _json(url, {'Referer': 'https://bbs.mihoyo.com/'})


# ---- 列表抓取 ------------------------------------------------------------
def list_new(app, menu_id):
    """hoyowiki 列表：分页取全量"""
    out = []
    page = 1
    while page <= 60:
        d = new_api_json(app, 'search_insert_entry_page',
                         {'menu_id': menu_id, 'page_size': 100, 'page_num': page})
        lst = (d.get('data') or {}).get('list') or []
        if not lst:
            break
        out.extend(lst)
        if len(lst) < 100:
            break
        page += 1
        time.sleep(0.2)
    return out


def list_old(app_sn, channel_id):
    """sr_wiki 老接口：分类节点里的 list"""
    d = old_api_json(app_sn, 'home/content/list',
                     {'app_sn': app_sn, 'channel_id': channel_id})
    nodes = (d.get('data') or {}).get('list') or []
    out = []
    for node in nodes:
        out.extend(node.get('list') or [])
    return out


def entry_page(app, entry_page_id):
    """新接口条目详情"""
    d = new_api_json(app, 'entry_page_v2', {'entry_page_id': entry_page_id})
    return (d.get('data') or {}).get('page') or {}


# ---- 工具 ----------------------------------------------------------------
def _db_path(name):
    """db 数据文件路径（DB_DIR 可能被外部覆盖，运行时才拼）"""
    return os.path.join(DB_DIR, name)


def _strip_html(s):
    """去 HTML 标签 + 实体反转义 + 压缩空白（[详情] 悬浮标记一并去掉）"""
    if not s:
        return ''
    s = re.sub(r'<sup class="wiki-sup">.*?</sup>', '', s, flags=re.S)
    s = re.sub(r'<br\s*/?>', ' ', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    s = re.sub(r'[\s　]+', ' ', s)
    return s.strip()


def _save_image(data, fp, size=256, square=True):
    """下载图片落盘。
    square=True：中心裁剪成方图再缩放到 size（星魂/命座图标，只缩不放）；
    square=False：保持宽幅，只缩放到宽 size（绝区零影画要横切 6 段做进度条，
    必须保留原始宽高比）。优先用 Qt 图像（源码 / 打包后都有），失败则原样保存。"""
    try:
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt
        qim = QImage.fromData(data)
        if qim.isNull():
            raise ValueError('not an image')
        if square:
            side = min(qim.width(), qim.height())
            qim = qim.copy((qim.width() - side) // 2, (qim.height() - side) // 2,
                           side, side)
            if side > size:
                qim = qim.scaled(size, size, Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation)
        elif qim.width() > size:
            tw = size
            th = max(1, int(round(qim.height() * size / qim.width())))
            qim = qim.scaled(tw, th, Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
        if qim.save(fp, 'PNG'):
            return True
        raise ValueError('save failed')
    except Exception:
        with open(fp, 'wb') as f:
            f.write(data)
        return False


def fetch_const_image(url, wide=False):
    """下载星魂/影画图。宽幅影画（绝区零）走 CDN 缩放参数，
    1.8MB 原图降到 ~280KB，失败自动回退原图。"""
    if wide:
        try:
            d = _http(url + '?x-oss-process=image/resize,w_720',
                      {'Referer': 'https://baike.mihoyo.com/'},
                      retries=1, timeout=45)
            if img_ext(d):
                return d
        except Exception:
            pass
    return _http(url, {'Referer': 'https://baike.mihoyo.com/'},
                 retries=2, timeout=60)


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', '', (name or '').strip())


def is_png(data):
    return data[:8] == b'\x89PNG\r\n\x1a\n'


def img_ext(data):
    """识别图片格式：返回 'png' / 'jpg' / None（米游社部分素材是 JPEG）"""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    return None


def load_manifest():
    if os.path.isfile(MANIFEST):
        try:
            with open(MANIFEST, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_manifest(m):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=1)


def days_since(datestr):
    """'YYYY-MM-DD HH:MM' 距现在多少天；解析失败返回极大值"""
    if not datestr:
        return 9999.0
    try:
        t = time.strptime(str(datestr)[:16], '%Y-%m-%d %H:%M')
    except Exception:
        return 9999.0
    return (time.time() - time.mktime(t)) / 86400.0


def last_check_str(m=None):
    """manifest 里记录的上次检查时间（meta.last_check）"""
    m = m if m is not None else load_manifest()
    return ((m.get('meta') or {}).get('last_check')) or ''


def touch_last_check(m):
    mg = m.setdefault('meta', {})
    mg['last_check'] = time.strftime('%Y-%m-%d %H:%M')


def local_files(dirkey, sub, base=None):
    d = os.path.join(base or ASSETS, dirkey, sub)
    if not os.path.isdir(d):
        return set()
    out = set()
    for f in os.listdir(d):
        low = f.lower()
        if low.endswith('.png') or low.endswith('.jpg'):
            out.add(os.path.splitext(f)[0])
    return out


def load_alias():
    if os.path.isfile(ALIAS_FILE):
        try:
            with open(ALIAS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ---- 名称归一化（与 build_alias.py 保持一致）---------------------------
try:
    import zhconv
    def H(s):
        return zhconv.convert(s, 'zh-hans')
except Exception:
    def H(s):
        return s

ROMAN = {'Ⅰ': 'I', 'Ⅱ': 'II', 'Ⅲ': 'III', 'Ⅳ': 'IV', 'Ⅴ': 'V',
         'ⅰ': 'i', 'ⅱ': 'ii', 'ⅲ': 'iii', 'ⅳ': 'iv', 'ⅴ': 'v'}
NUMWORD = {'參': '叁', '貳': '贰', '壹': '壹', '叁': '叁', '参': '叁',
           '贰': '贰', '肆': '肆', '伍': '伍'}
CHAR_EQ = {'祕': '秘', '傑': '杰', '佈': '布', '裏': '里', '爲': '为',
           '牠': '它', '衆': '众', '縱': '纵', '蹟': '迹'}


def norm(s):
    if not s:
        return ''
    s = H(str(s))
    for a, b in ROMAN.items():
        s = s.replace(a, b)
    for a, b in NUMWORD.items():
        s = s.replace(a, b)
    for a, b in CHAR_EQ.items():
        s = s.replace(a, b)
    s = re.sub(r'[·・‧\s　]', '', s)
    s = s.replace('「', '').replace('」', '')
    s = s.replace('【', '').replace('】', '')
    s = s.replace('★', '')
    return s


def strip_suffix(s):
    return re.sub(r'【[^】]*】', '', s)


def auto_match(local, remote):
    ln = norm(local)
    if not ln:
        return None
    exact = [r for r in remote if norm(r) == ln]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return min(exact, key=len)
    cand = []
    for r in remote:
        rn = norm(strip_suffix(r))
        if not rn:
            continue
        if rn.startswith(ln) or ln.startswith(rn):
            cand.append(r)
    if len(cand) == 1:
        return cand[0]
    if len(cand) > 1:
        return min(cand, key=lambda r: len(norm(strip_suffix(r))))
    return None


# ---- 各游戏的素材收集 ----------------------------------------------------
def collect_new(app, cat, menu_id):
    """返回 [(显示名, 文件名, 图片url, 条目id)]"""
    items = []
    if app == 'genshin' and cat == 'relics':
        # 原神遗器是套装，5 个部位要从详情页拆
        for it in list_new(app, menu_id):
            name = safe_name(it.get('name'))
            eid = it.get('entry_page_id')
            if not name or not eid:
                continue
            page = entry_page(app, eid)
            got = 0
            for mod in (page.get('modules') or []):
                mname = mod.get('name')
                if mname not in GENSHIN_SLOTS:
                    continue
                url = None
                for comp in (mod.get('components') or []):
                    raw = comp.get('data')
                    if not isinstance(raw, str) or 'icon_url' not in raw:
                        continue
                    try:
                        j = json.loads(raw)
                    except Exception:
                        continue
                    if j.get('icon_url'):
                        url = j['icon_url']
                        break
                if url:
                    idx = GENSHIN_SLOTS.index(mname) + 1
                    items.append((f'{name} {mname}', f'{name}_{idx}', url, f'{eid}#{idx}'))
                    got += 1
            if not got:
                items.append((name, name, it.get('icon_url'), str(eid)))
            time.sleep(0.15)
        return items

    seen = set()
    for it in list_new(app, menu_id):
        name = safe_name(it.get('name'))
        url = it.get('icon_url')
        eid = it.get('entry_page_id')
        if not name or not url:
            continue
        # 同名条目只保留第一条（官网存在同名不同 id 的情况）
        if name in seen:
            continue
        seen.add(name)
        items.append((name, name, url, str(eid)))
    return items


def collect_old(app, cat, channel_id):
    items = []
    seen = set()
    for it in list_old('sr_wiki', channel_id):
        name = safe_name(it.get('title'))
        url = it.get('icon')
        eid = it.get('content_id')
        if not name or not url:
            continue
        # 官网存在同名多条（如「开拓者•记忆」男/女两个条目），
        # 文件名会互相覆盖；只保留第一条，保证 URL 稳定、可幂等跳过。
        if name in seen:
            continue
        seen.add(name)
        items.append((name, name, url, str(eid)))
    return items


def _dedupe_idx(imgs):
    """官网同一格可能重复出现（多元素变体 / 时装变体），只保留第一条"""
    out, seen = [], set()
    for idx, url in imgs:
        if idx in seen:
            continue
        seen.add(idx)
        out.append((idx, url))
    return out


# ---- 星魂 / 影画 / 命座 ---------------------------------------------------
def _const_hsr():
    """星铁星魂：content/info → rpg_new_tmp_content.modules['角色星魂']
    的 richText（6 张 data-image-url + 【N】标题 + 效果文字）"""
    out = {}
    for it in list_old('sr_wiki', 18):
        name = safe_name(it.get('title'))
        cid = it.get('content_id')
        if not name or not cid:
            continue
        try:
            d = old_api_json('sr_wiki', 'content/info',
                             {'app_sn': 'sr_wiki', 'content_id': cid})
        except Exception as e:
            log(f'  [星魂] {name} 详情失败: {e}')
            continue
        content = (d.get('data') or {}).get('content') or {}
        rpg = content.get('rpg_new_tmp_content') or {}
        imgs, names, desc = [], {}, {}
        for m in (rpg.get('modules') or []):
            if m.get('name') != '角色星魂':
                continue
            for comp in (m.get('components') or []):
                raw = comp.get('data')
                if not isinstance(raw, str):
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                txt = j.get('richText') or ''
                for i, u in enumerate(re.findall(r'data-image-url="([^"]+)"',
                                                 txt)[:6]):
                    imgs.append((i + 1, u))
                for hm in re.finditer(r'<h3[^>]*>(.*?)</h3>(.*?)(?=<h3|$)',
                                      txt, re.S):
                    t = _strip_html(hm.group(1))
                    mm = re.match(r'【\s*(\d)\s*】\s*(.*)$', t)
                    if not mm:
                        continue
                    lvl = mm.group(1)
                    names[lvl] = mm.group(2).strip()
                    pm = re.search(r'<p[^>]*>(.*?)</p>', hm.group(2), re.S)
                    if pm:
                        desc[lvl] = _strip_html(pm.group(1))
        if imgs:
            out[name] = {'id': str(cid), 'images': _dedupe_idx(imgs),
                         'desc': desc, 'names': names}
        time.sleep(0.1)
    return out


def _const_zzz():
    """绝区零影画：entry_page 的「影画展示1/2/3」宽幅插画 + LEVEL1-6 表"""
    out = {}
    for c in list_new('zzz', 43):
        name = safe_name(c.get('name'))
        eid = c.get('entry_page_id')
        if not name or not eid:
            continue
        try:
            pg = entry_page('zzz', eid)
        except Exception as e:
            log(f'  [影画] {name} 详情失败: {e}')
            continue
        imgs, names, desc = [], {}, {}
        for m in (pg.get('modules') or []):
            for comp in (m.get('components') or []):
                raw = comp.get('data')
                if not isinstance(raw, str):
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(j, dict):
                    continue
                for x in (j.get('list') or []):
                    if not isinstance(x, dict):
                        continue
                    if '影画展示' not in str(x.get('tab_name')):
                        continue
                    mm = re.search(r'(\d+)', str(x.get('tab_name')))
                    if mm and x.get('image'):
                        imgs.append((int(mm.group(1)), x['image']))
                for t in (j.get('tables') or []):
                    for row in (t.get('row') or []):
                        cells = [c2 for c2 in row if isinstance(c2, str)]
                        if len(cells) < 2 or 'LEVEL' not in cells[0]:
                            continue
                        mm = re.search(r'LEVEL\s*(\d)', cells[0])
                        if not mm:
                            continue
                        lvl = mm.group(1)
                        nm = re.sub(r'^LEVEL\s*\d+\s*', '',
                                    _strip_html(cells[0])).strip()
                        if nm:
                            names[lvl] = nm
                        desc[lvl] = _strip_html(cells[1])
        if imgs:
            out[name] = {'id': str(eid), 'images': _dedupe_idx(imgs),
                         'desc': desc, 'names': names}
        time.sleep(0.1)
    return out


def _const_genshin():
    """原神命座：entry_page「命之座」表行 HTML 里 6 个 data-image-url"""
    out = {}
    for c in list_new('genshin', 25):
        name = safe_name(c.get('name'))
        eid = c.get('entry_page_id')
        if not name or not eid:
            continue
        try:
            pg = entry_page('genshin', eid)
        except Exception as e:
            log(f'  [命座] {name} 详情失败: {e}')
            continue
        imgs, names, desc = [], {}, {}
        seq = 0
        for m in (pg.get('modules') or []):
            if m.get('name') != '命之座':
                continue
            for comp in (m.get('components') or []):
                raw = comp.get('data')
                if not isinstance(raw, str):
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                for t in (j.get('tables') or []):
                    for row in (t.get('row') or []):
                        cells = [c for c in row if isinstance(c, str)]
                        imgc = next((c for c in cells
                                     if 'data-image-url' in c), None)
                        if imgc is None:
                            continue
                        segs = [s for s in re.split(r'<hr\s*/?>', imgc)
                                if 'data-image-url' in s]
                        if len(segs) > 1:
                            # 布局A：一个单元格里用 <hr> 串起 6 个命座
                            for seg in segs:
                                su = re.findall(r'data-image-url="([^"]+)"',
                                                seg)
                                if not su or seq >= 6:
                                    continue
                                seq += 1
                                imgs.append((seq, su[0]))
                                ps = [_strip_html(p) for p in
                                      re.findall(r'<p[^>]*>(.*?)</p>', seg,
                                                 re.S)]
                                ps = [p for p in ps if p]
                                if ps:
                                    names[str(seq)] = ps[0]
                                    if len(ps) > 1:
                                        desc[str(seq)] = ' '.join(ps[1:])
                        else:
                            # 布局B：一行一个命座（旅行者各元素变体）
                            if seq >= 6:
                                continue
                            su = re.findall(r'data-image-url="([^"]+)"', imgc)
                            if not su:
                                continue
                            seq += 1
                            imgs.append((seq, su[0]))
                            nm = _strip_html(imgc)
                            if nm:
                                names[str(seq)] = nm
                            others = [_strip_html(c) for c in cells
                                      if c is not imgc]
                            others = [o for o in others if o]
                            if others:
                                desc[str(seq)] = ' '.join(others)
        if imgs:
            out[name] = {'id': str(eid), 'images': _dedupe_idx(imgs),
                         'desc': desc, 'names': names}
        time.sleep(0.1)
    return out


CONST_COLLECT = {'hsr': _const_hsr, 'zzz': _const_zzz, 'genshin': _const_genshin}


def sync_constellations(games=None, dry=False, quiet=False):
    """同步星魂/影画/命座：图片 → assets_wiki/constellations/<game>/，
    文字 → db/星魂库.json；官网旧库（assets/constellations）已有的沿用不重下。"""
    games = games or ['hsr', 'zzz', 'genshin']
    man = load_manifest()
    cm = man.setdefault('constellations', {})
    stats = Counter()
    for game in games:
        collect = CONST_COLLECT.get(game)
        if not collect:
            continue
        log(f'== {game} 星魂/影画/命座 ==', quiet)
        try:
            recs = collect()
        except Exception as e:
            log(f'  抓取失败: {e}', quiet)
            stats['fail'] += 1
            continue
        gm = cm.setdefault(game, {})
        wiki_dir = os.path.join(WIKI_ASSETS, 'constellations', game)
        legacy_dir = os.path.join(ASSETS, 'constellations', game)
        # 绝区零影画是宽幅插画，界面要横切 6 段做进度条，保持原始宽高比
        wide = (game == 'zzz')
        n_new = n_skip = n_local = 0
        for name, rec in recs.items():
            old = gm.get(name) or {}
            old_imgs = {im.get('idx'): im
                        for im in (old.get('images') or [])}
            entry = {'id': rec['id'], 'desc': rec.get('desc') or {},
                     'names': rec.get('names') or {}, 'images': []}
            changed = False
            seen_idx = set()
            for idx, url in rec['images']:
                if idx in seen_idx:      # 官网同一格可能重复出现（多元素变体等）
                    continue
                seen_idx.add(idx)
                fn = f'{name}_{idx}'
                oi = old_imgs.get(idx)
                if oi and oi.get('url') == url and os.path.isfile(
                        os.path.join(HERE, oi.get('file') or '')):
                    entry['images'].append(oi)
                    n_skip += 1
                    continue
                # 本地旧库已有同名图案（历史下载的全套星魂/命座）→ 沿用不重下
                legacy = os.path.join(legacy_dir, fn + '.png')
                if os.path.isfile(legacy):
                    entry['images'].append({
                        'idx': idx,
                        'file': os.path.relpath(legacy, HERE).replace('\\', '/'),
                        'url': url, 'src': 'local'})
                    changed = True
                    n_local += 1
                    continue
                if dry:
                    n_new += 1
                    continue
                try:
                    data = fetch_const_image(url, wide=wide)
                except Exception as e:
                    log(f'    {fn} 下载失败: {e}', quiet)
                    stats['fail'] += 1
                    continue
                if not img_ext(data):
                    log(f'    {fn} 非图片', quiet)
                    stats['fail'] += 1
                    continue
                os.makedirs(wiki_dir, exist_ok=True)
                fp = os.path.join(wiki_dir, fn + '.png')
                # 480 宽 → 每段 80px，仍是界面 38px 的 2 倍
                _save_image(data, fp, size=480 if wide else 256, square=not wide)
                entry['images'].append({
                    'idx': idx,
                    'file': os.path.relpath(fp, HERE).replace('\\', '/'),
                    'url': url,
                    'sha1': hashlib.sha1(data).hexdigest()})
                changed = True
                n_new += 1
                time.sleep(0.05)
            if entry['images'] and (changed or not old):
                entry['updated'] = time.strftime('%Y-%m-%d %H:%M')
                gm[name] = entry
        log(f'  官网角色 {len(recs)} 个 | 新增图 {n_new}，'
            f'本地沿用 {n_local}，未变 {n_skip}', quiet)
        stats['new'] += n_new
        stats['local'] += n_local
    if not dry:
        save_manifest(man)
        export_eido_db(cm, quiet=quiet)
    return stats


def export_eido_db(cm, quiet=False):
    """manifest 的 constellations → db/星魂库.json（按游戏分键的人读版）"""
    out = {}
    for game, chars in cm.items():
        out[game] = {}
        for name, ent in chars.items():
            imgs = [{'file': im.get('file'), 'url': im.get('url'),
                     'sha1': im.get('sha1', '')}
                    for im in (ent.get('images') or [])]
            rec = {'images': imgs, 'desc': ent.get('desc') or {}}
            if ent.get('names'):
                rec['names'] = ent['names']
            out[game][name] = rec
    fp = _db_path('星魂库.json')
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f'已导出 {fp}（{sum(len(v) for v in out.values())} 个角色）', quiet)


# ---- 写入 ----------------------------------------------------------------
def write_asset(target_dir, filename, url, dry=False):
    """下载素材到 target_dir，返回 (sha1, 实际文件名, data)"""
    if dry:
        return 'plan', filename + '.png', b''
    try:
        data = _http(url, {'Referer': 'https://baike.mihoyo.com/'}, retries=2, timeout=60)
    except Exception as e:
        return f'fail:{e}', filename + '.png', b''
    ext = img_ext(data)
    if not ext:
        return 'notpng', filename + '.png', b''
    real = filename + '.' + ext
    fp = os.path.join(target_dir, real)
    with open(fp, 'wb') as f:
        f.write(data)
    # 旧版本可能留了同名 .png，扩展名变化时清掉避免歧义
    other = os.path.join(target_dir, filename + ('.jpg' if ext == 'png' else '.png'))
    if os.path.isfile(other) and other != fp:
        try:
            os.remove(other)
        except Exception:
            pass
    return hashlib.sha1(data).hexdigest(), real, data


# ---- 孤儿素材清理 --------------------------------------------------------
def prune_orphans(dry=False, quiet=False):
    """覆盖/改名后残留的孤儿素材清理：manifest 已不引用、
    却还躺在 assets_wiki/ 里的图片文件 → 删除（或 dry 只列出）。
    只动 assets_wiki/（官网库，可随时重下），绝不碰 assets/（本地旧库）。"""
    man = load_manifest()
    ref = set()
    # 星魂/影画/命座：man['constellations'][game][角色].images[].file
    for game, chars in (man.get('constellations') or {}).items():
        if not isinstance(chars, dict):
            continue
        for ce in chars.values():
            for im in (ce.get('images') or []):
                if im.get('file'):
                    ref.add(str(im['file']).replace('\\', '/'))
    # 图标：man[app][cat][文件名].file
    for app, gm in man.items():
        if app in ('meta', 'constellations'):
            continue
        if isinstance(gm, dict):
            for cat, entries in gm.items():
                if isinstance(entries, dict):
                    for ent in entries.values():
                        if isinstance(ent, dict) and ent.get('file'):
                            ref.add(str(ent['file']).replace('\\', '/'))
    removed = missed = 0
    for sub in ('characters', 'weapons', 'relics', 'constellations'):
        base = os.path.join(WIKI_ASSETS, sub)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if not f.lower().endswith(('.png', '.jpg')):
                    continue
                rel = os.path.relpath(os.path.join(root, f),
                                      HERE).replace('\\', '/')
                if rel in ref:
                    continue
                if dry:
                    log(f'  [孤儿·只列出] {rel}', quiet)
                    removed += 1
                    continue
                try:
                    os.remove(os.path.join(root, f))
                    removed += 1
                except Exception as e:
                    missed += 1
                    log(f'  [删除失败] {rel}: {e}', quiet)
    log(f'孤儿素材清理：{"列出" if dry else "删除"} {removed} 个'
        + (f'，删除失败 {missed} 个' if missed else ''), quiet)
    return removed


# ---- 更新 ----------------------------------------------------------------
def update_game(app, dry=False, quiet=False):
    if app in NEW_API:
        dirname, menus = NEW_API[app]
        get = lambda cat, mid: collect_new(app, cat, mid)
    else:
        dirname, menus = OLD_API[app]
        get = lambda cat, mid: collect_old(app, cat, mid)

    log(f'== {app} ({dirname}) ==', quiet)
    manifest = load_manifest()
    gm = manifest.setdefault(app, {})
    stats = Counter()

    for cat, mid in menus.items():
        try:
            items = get(cat, mid)
        except Exception as e:
            log(f'  [{cat}] 抓取失败: {e}', quiet)
            continue
        wiki_dir = os.path.join(WIKI_ASSETS, CAT_DIR[cat], dirname)
        os.makedirs(wiki_dir, exist_ok=True)
        cat_man = gm.setdefault(cat, {})
        for disp, fn, url, eid in items:
            fp_png = os.path.join(wiki_dir, fn + '.png')
            fp_jpg = os.path.join(wiki_dir, fn + '.jpg')
            fp = fp_png if os.path.isfile(fp_png) else fp_jpg
            old = cat_man.get(fn)
            # 官网的 content_id / icon_url 每次请求都会变（CDN 路径带随机串），
            # 所以下载后按内容 sha1 判断是否真的更新过；内容相同就不动文件。
            if old and old.get('url') == url and os.path.isfile(fp):
                stats['skip'] += 1
                continue
            if dry:
                stats['new'] += 1
                continue
            res, real, data = write_asset(wiki_dir, fn, url, dry=False)
            if res.startswith('fail') or res == 'notpng':
                stats['fail'] += 1
                continue
            same = bool(old) and old.get('sha1') == res
            cat_man[fn] = {
                'id': eid, 'url': url, 'disp': disp,
                'sha1': res,
                'file': os.path.relpath(os.path.join(wiki_dir, real), HERE).replace('\\', '/'),
                'updated': time.strftime('%Y-%m-%d %H:%M'),
            }
            if same:
                stats['same'] += 1
            else:
                stats['new'] += 1
            time.sleep(0.06)
        log(f'  [{cat}] 官网条目 {len(items)} 项', quiet)
    log(f'  结果: 新增/更新 {stats["new"]}，内容未变 {stats["same"]}，'
        f'跳过 {stats["skip"]}，失败 {stats["fail"]}', quiet)
    if not dry:
        save_manifest(manifest)
    return stats


# ---- 重建名字映射表 ------------------------------------------------------
def rebuild_alias(quiet=False):
    """用 assets/ (本地旧名) + assets_wiki/ (官网名) 重建 db/名字映射.json"""
    result = {}
    for game, dirname in GAME_DIR.items():
        result[game] = {}
        for cat in CAT_DIR:
            remote = sorted(local_files(CAT_DIR[cat], dirname, WIKI_ASSETS))
            d = os.path.join(ASSETS, CAT_DIR[cat], dirname)
            local = sorted(local_files(CAT_DIR[cat], dirname, ASSETS)) \
                if os.path.isdir(d) else []
            manual = MANUAL.get(game, {}).get(cat, {})
            m = {}
            unmatched = []
            for l in local:
                if l in manual:
                    m[l] = manual[l]
                    continue
                hit = auto_match(l, remote)
                if hit:
                    m[l] = hit
                else:
                    unmatched.append(l)
            # 官网新增（本地没有的）也登记，新角色直接出现在角色库
            for r in remote:
                if r not in m.values():
                    m.setdefault(r, r)
            result[game][cat] = m
            if unmatched:
                log(f'  [对齐] {game}/{cat} 未匹配 {len(unmatched)}: '
                    f'{"、".join(unmatched[:8])}', quiet)
    os.makedirs(os.path.dirname(ALIAS_FILE), exist_ok=True)
    with open(ALIAS_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    log(f'已重建 {ALIAS_FILE}', quiet)


# ---- 套装效果 ------------------------------------------------------------
def _set_effects_genshin():
    """原神：entry_page 基础信息 list 的「2/4件套效果」"""
    out = {}
    for s in list_new('genshin', 218):
        name = safe_name(s.get('name'))
        eid = s.get('entry_page_id')
        if not name or not eid:
            continue
        try:
            pg = entry_page('genshin', eid)
        except Exception as e:
            log(f'  [套装] {name} 详情失败: {e}')
            continue
        eff = {}
        for m in (pg.get('modules') or []):
            for comp in (m.get('components') or []):
                raw = comp.get('data')
                if not isinstance(raw, str) or '件套效果' not in raw:
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                for kv in (j.get('list') or []):
                    if not isinstance(kv, dict):
                        continue
                    # 官网键值带多余空格（"2件套效果 " / " 4件套效果 "）
                    key = str(kv.get('key') or '').strip()
                    val = kv.get('value')
                    if key not in ('2件套效果', '4件套效果') or not val:
                        continue
                    piece = '2件套' if key.startswith('2') else '4件套'
                    txt = _strip_html(val[0] if isinstance(val, list)
                                      else str(val))
                    if txt:
                        eff[piece] = txt
        if eff:
            out[name] = eff
        time.sleep(0.1)
    return out


def _set_effects_zzz():
    """绝区零：entry_page tables header==['二件套','四件套']"""
    out = {}
    for s in list_new('zzz', 46):
        name = H(safe_name(s.get('name')))
        eid = s.get('entry_page_id')
        if not name or not eid:
            continue
        try:
            pg = entry_page('zzz', eid)
        except Exception as e:
            log(f'  [套装] {name} 详情失败: {e}')
            continue
        eff = {}
        for m in (pg.get('modules') or []):
            for comp in (m.get('components') or []):
                raw = comp.get('data')
                if not isinstance(raw, str) or '二件套' not in raw:
                    continue
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                for t in (j.get('tables') or []):
                    hdr = t.get('header') or []
                    if not any('件套' in str(h) for h in hdr):
                        continue
                    for row in (t.get('row') or []):
                        cells = [c for c in row if isinstance(c, str)]
                        if len(cells) >= 2:
                            eff['2件套'] = H(_strip_html(cells[0]))
                            eff['4件套'] = H(_strip_html(cells[1]))
        if eff:
            out[name] = eff
        time.sleep(0.1)
    return out


def _set_effects_hsr():
    """星铁：content/info contents 文本段的「二件套：」「四件套：」
    （位面饰品只有二件套，且文本会重复一遍）"""
    out = {}
    for s in list_old('sr_wiki', 30):
        name = safe_name(s.get('title'))
        cid = s.get('content_id')
        if not name or not cid:
            continue
        try:
            d = old_api_json('sr_wiki', 'content/info',
                             {'app_sn': 'sr_wiki', 'content_id': cid})
        except Exception as e:
            log(f'  [套装] {name} 详情失败: {e}')
            continue
        content = (d.get('data') or {}).get('content') or {}
        txt = ''
        for c in (content.get('contents') or []):
            t = _strip_html(c.get('text') or '')
            if '二件套' in t:
                txt = t
                break
        eff = {}
        m2 = re.search(r'二件套：(.+?)(?=四件套：|二件套：|查看全部|$)', txt,
                       re.S)
        m4 = re.search(r'四件套：(.+?)(?=查看全部|$)', txt, re.S)
        if m2:
            eff['2件套'] = m2.group(1).strip()
        if m4:
            eff['4件套'] = m4.group(1).strip()
        if eff:
            out[name] = eff
        time.sleep(0.1)
    return out


def sync_set_effects(dry=False, quiet=False):
    """从官网填充 db/套装库.json：新增套装整条加入，已有套装只补空效果
    （不覆盖用户手改）；绝区零繁体统一转简体。"""
    fp = _db_path('套装库.json')
    db = {}
    if os.path.isfile(fp):
        try:
            with open(fp, encoding='utf-8') as f:
                db = json.load(f)
        except Exception:
            db = {}
    for g in ('星铁', '原神', '绝区零'):
        db.setdefault(g, {})
    stats = Counter()
    for game, fn in (('星铁', _set_effects_hsr),
                     ('原神', _set_effects_genshin),
                     ('绝区零', _set_effects_zzz)):
        try:
            recs = fn()
        except Exception as e:
            log(f'  [套装效果] {game} 抓取失败: {e}', quiet)
            stats['fail'] += 1
            continue
        n_new = n_fill = 0
        for name, eff in recs.items():
            ent = db[game].get(name)
            if ent is None:
                db[game][name] = {'alias': [], 'effects': dict(eff)}
                n_new += 1
                continue
            e2 = ent.setdefault('effects', {})
            for piece, txt in eff.items():
                if not e2.get(piece):
                    e2[piece] = txt
                    n_fill += 1
                elif game == '绝区零' and e2.get(piece) != H(e2[piece]):
                    e2[piece] = H(e2[piece])   # 历史繁体转简体
        log(f'  [套装效果] {game}: 官网 {len(recs)} 套 | '
            f'新增 {n_new}，补空 {n_fill}', quiet)
        stats['new'] += n_new
        stats['fill'] += n_fill
    if not dry:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(db, f, ensure_ascii=False, indent=1)
        log(f'已更新 {fp}', quiet)
    return stats


# ---- 属性（命途/元素/定位）同步 -------------------------------------------
def _parse_filter(texts):
    """['星级/五星','命途/虚无','属性/火'] -> {'星级':'五星','命途':'虚无','属性':'火'}"""
    out = {}
    if isinstance(texts, str):
        try:
            texts = json.loads(texts)
        except Exception:
            texts = []
    for t in (texts or []):
        if isinstance(t, str) and '/' in t:
            k, v = t.split('/', 1)
            out[k] = v
    return out


def _filter_from_item(it, menu_id):
    """从列表 item 取结构化字段：兼容两种结构
       - 新接口：ext.fe_ext.c_<menu>.filter.text
       - 星铁老接口：ext.c_<menu>.filter.text（直挂）
    """
    ext = it.get('ext')
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except Exception:
            ext = {}
    if not isinstance(ext, dict):
        return {}
    node = None
    fe = ext.get('fe_ext')
    if isinstance(fe, dict):
        node = fe.get(f'c_{menu_id}')
    if node is None:
        node = ext.get(f'c_{menu_id}')
    if isinstance(node, dict):
        ft = node.get('filter', {}).get('text')
        if ft:
            return _parse_filter(ft)
    return {}


def _parse_genshin_pos(val):
    """原神 定位自由文本 '水元素辅助角色' -> {'元素':'水','定位':'辅助'}"""
    if not val:
        return {}
    m = re.match(r'^([一-龥]{1,2})元素(.+)$', val)
    if m:
        role = m.group(2)
        role = re.sub(r'型角色$', '', role)   # 输出型角色 -> 输出型
        role = re.sub(r'角色$', '', role)      # 输出型 -> 输出 / 辅助角色 -> 辅助
        return {'元素': m.group(1), '定位': role or val}
    return {'定位': val}


def _genshin_attr(eid):
    """原神：元素从 entry_page 的 ext.fe_ext.c_25.filter 取（元素/雷），
    定位从「基础信息」模块的 attr 取；两者同一次 entry_page 请求。
    官网无独立的「元素」展示字段，元素只藏在列表/页面 filter 里。"""
    page = entry_page('genshin', eid)
    # 元素：entry_page.ext.fe_ext.c_25.filter -> ['…','元素/雷','…']
    elem = None
    ext = page.get('ext')
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except Exception:
            ext = {}
    if isinstance(ext, dict):
        fe = ext.get('fe_ext') or {}
        if isinstance(fe, str):
            try:
                fe = json.loads(fe)
            except Exception:
                fe = {}
        c25 = (fe or {}).get('c_25') or {}
        if isinstance(c25, dict):
            ft = c25.get('filter', {}).get('text')
            if ft:
                pf = _parse_filter(ft)
                elem = pf.get('元素')
    # 定位：基础信息模块 attr 的「定位」值
    loc = None
    for mod in page.get('modules') or []:
        if mod.get('name') == '基础信息':
            for c in mod.get('components') or []:
                data = c.get('data')
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        continue
                if isinstance(data, dict):
                    attr = data.get('attr')
                    if isinstance(attr, list):
                        for a in attr:
                            if a.get('key') == '定位':
                                loc = (a.get('value') or [''])[0]
    return {'元素': elem or '—', '定位': loc or '—'}


def sync_attrs(games=None, dry=False, quiet=False):
    """同步三游戏角色属性（命途/元素/定位）→ db/属性库.json。

    星铁：list_old 的 c_18.filter 直接给 命途/属性；
    绝区零：list_new 的 c_43.filter 给 属性/特性；
    原神：list_new 拿 entry_page_id，再抓 entry_page——
          元素从 ext.fe_ext.c_25.filter 的「元素/XX」取，定位从基础信息 attr 取。
    均为纯文字，无图片下载；可随 --all 周期刷新，也可 --attrs 单独跑。
    """
    games = games or ['hsr', 'zzz', 'genshin']
    out = {}
    if 'hsr' in games:
        log('== 星铁 命途/元素 ==', quiet)
        d = {}
        for it in list_old('sr_wiki', 18):
            title = it.get('title') or it.get('name')
            pf = _filter_from_item(it, 18)
            if title and '命途' in pf and '属性' in pf:
                d[title] = {'命途': pf['命途'], '元素': pf['属性']}
        out['hsr'] = d
        log(f'  星铁 {len(d)} 个角色', quiet)
    if 'zzz' in games:
        log('== 绝区零 属性/特性 ==', quiet)
        d = {}
        for mid in (43, 45, 46):
            for it in list_new('zzz', mid):
                name = it.get('name')
                pf = _filter_from_item(it, mid)
                if name and '属性' in pf and '特性' in pf:
                    d[name] = {'元素': pf['属性'], '定位': pf['特性']}
        out['zzz'] = d
        log(f'  绝区零 {len(d)} 个角色', quiet)
    if 'genshin' in games:
        log('== 原神 元素/定位 ==', quiet)
        d, n = {}, 0
        for it in list_new('genshin', 25):
            name = it.get('name')
            if not name or '预告' in name or '旅行者·' in name:
                continue
            eid = it.get('entry_page_id')
            if not eid:
                continue
            try:
                a = _genshin_attr(eid)
            except Exception as e:
                log(f'    {name} 失败: {e}', quiet)
                continue
            if a.get('元素') != '—' or a.get('定位') != '—':
                d[name] = a
                n += 1
            time.sleep(0.12)
        out['genshin'] = d
        log(f'  原神 {n} 个角色', quiet)
    if not dry:
        fp = _db_path('属性库.json')
        # 合并写入：单游戏同步时不要清空其它游戏已有的数据
        existing = {}
        if os.path.isfile(fp):
            try:
                with open(fp, encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        existing.update(out)
        out = existing
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        log(f'已导出 {fp}（{sum(len(v) for v in out.values())} 个角色）', quiet)
    return out


# ---- 比对模式 ------------------------------------------------------------
def do_compare(quiet=False):
    global COMPARE_ROWS
    COMPARE_ROWS = []
    log('=== 米游社官方素材 vs 本地 assets 覆盖比对 ===\n', quiet)
    alias = load_alias()
    for app in ['hsr', 'zzz', 'genshin']:
        if app in NEW_API:
            dirname, menus = NEW_API[app]
            get = lambda cat, mid: collect_new(app, cat, mid)
        else:
            dirname, menus = OLD_API[app]
            get = lambda cat, mid: collect_old(app, cat, mid)
        log(f'--- {app} ---', quiet)
        for cat, mid in menus.items():
            try:
                items = get(cat, mid)
            except Exception as e:
                log(f'  [{cat}] 抓取失败: {e}', quiet)
                COMPARE_ROWS.append({'game': app, 'cat': cat, 'remote': 0,
                                    'have': 0, 'wiki': 0, 'missing': 0,
                                    'missing_names': f'抓取失败: {e}'})
                continue
            remote = {fn for _, fn, _, _ in items}
            wiki = local_files(CAT_DIR[cat], dirname, WIKI_ASSETS)
            have = local_files(CAT_DIR[cat], dirname, ASSETS)
            amap = {}
            for g, cats in alias.items():
                for c, mm in cats.items():
                    if c == cat:
                        for k, v in mm.items():
                            amap[k] = v
            covered = {amap.get(x, x) for x in have}
            missing = sorted(remote - covered)
            log(f'  [{cat}] 官网 {len(remote)} 项 | 本地旧库 {len(have)} | '
                f'官方库 {len(wiki)} | 映射后仍缺 {len(missing)}', quiet)
            if missing:
                log('     缺: ' + '、'.join(missing[:16]) + ('…' if len(missing) > 16 else ''), quiet)
            COMPARE_ROWS.append({'game': app, 'cat': cat,
                                'remote': len(remote), 'have': len(have),
                                'wiki': len(wiki), 'missing': len(missing),
                                'missing_names': ('、'.join(missing[:20]) or '—')})
        log('', quiet)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--game', choices=['hsr', 'zzz', 'genshin'], help='只更新某个游戏')
    ap.add_argument('--all', action='store_true', help='更新全部三个游戏')
    ap.add_argument('--dry-run', action='store_true', help='只看不下载')
    ap.add_argument('--compare', action='store_true', help='比对本地与官网覆盖差异')
    ap.add_argument('--constellations', action='store_true',
                    help='只同步星魂/影画/命座图案与说明')
    ap.add_argument('--set-effects', action='store_true',
                    help='只从官网填充套装效果（db/套装库.json）')
    ap.add_argument('--attrs', action='store_true',
                    help='只同步角色属性（命途/元素/定位 → db/属性库.json）')
    ap.add_argument('--prune', action='store_true',
                    help='清理 assets_wiki 里 manifest 已不引用的孤儿素材（不联网）')
    ap.add_argument('--quiet', action='store_true', help='静默模式（日志写 update.log）')
    ap.add_argument('--no-alias', action='store_true', help='不重建名字映射表')
    ap.add_argument('--exit-code', action='store_true', help='用退出码表示成功/失败')
    ap.add_argument('--force', action='store_true',
                    help='忽略时间周期强制更新（默认距上次检查不足 --max-age 天则跳过）')
    ap.add_argument('--max-age', type=int, default=30,
                    help='更新周期（天），默认 30；按电脑系统时间与上次检查时间比较')
    args = ap.parse_args(argv)
    quiet = args.quiet

    log(f'米游社素材更新 · {datetime.datetime.now():%Y-%m-%d %H:%M:%S}', quiet)
    try:
        if args.compare:
            do_compare(quiet)
            # 覆盖比对顺带列出可清理的孤儿素材（只看不删）
            try:
                prune_orphans(dry=True, quiet=quiet)
            except Exception as e:
                log(f'  孤儿素材列出失败: {e}', quiet)
            return 0

        # 只清理孤儿素材：不联网、不受更新周期限制
        if args.prune and not (args.all or args.game or args.constellations
                               or args.set_effects):
            return 0 if prune_orphans(dry=args.dry_run, quiet=quiet) >= 0 else 1

        # 按电脑系统时间判断是否到了更新周期：
        # 距上次检查不足 max-age 天就整体跳过（未变更素材本来也会跳过，这里连官网都不请求）
        man = load_manifest()
        last = last_check_str(man)
        age = days_since(last)
        if not args.dry_run and not args.force and age < args.max_age:
            log(f'上次检查 {last or "（无记录）"}，距今 {age:.1f} 天，'
                f'小于周期 {args.max_age} 天，本次跳过（--force 可强制执行）', quiet)
            return 0

        targets = ['hsr', 'zzz', 'genshin'] if (args.all or not args.game) else [args.game]
        total_fail = 0
        # 单独指定 --constellations/--set-effects 时不再跑全套图标更新
        skip_games = (args.constellations or args.set_effects) and not (args.all or args.game)
        if not skip_games:
            for app in targets:
                try:
                    st = update_game(app, dry=args.dry_run, quiet=quiet)
                    total_fail += st['fail']
                except Exception as e:
                    log(f'  {app} 更新失败: {e}', quiet)
                    total_fail += 1
                time.sleep(0.5)

        # 星魂/影画/命座 + 套装效果：默认随 --all 一起跑，也可单独指定
        if args.constellations or args.all:
            cg = targets if (args.game or args.all) else None
            try:
                st = sync_constellations(cg, dry=args.dry_run, quiet=quiet)
                total_fail += st['fail']
            except Exception as e:
                log(f'  星魂/影画同步失败: {e}', quiet)
                total_fail += 1
        if args.set_effects or args.all:
            try:
                st = sync_set_effects(dry=args.dry_run, quiet=quiet)
                total_fail += st['fail']
            except Exception as e:
                log(f'  套装效果同步失败: {e}', quiet)
                total_fail += 1
        if args.attrs or args.all:
            try:
                sync_attrs(targets if (args.game or args.all) else None,
                           dry=args.dry_run, quiet=quiet)
            except Exception as e:
                log(f'  属性同步失败: {e}', quiet)
                total_fail += 1

        if not args.dry_run and not args.no_alias:
            try:
                rebuild_alias(quiet)
            except Exception as e:
                log(f'  名字映射重建失败: {e}', quiet)
                total_fail += 1

        # 覆盖/改名后的残留清理：正式更新跑完顺手删掉孤儿素材
        if not args.dry_run and not skip_games:
            try:
                prune_orphans(dry=False, quiet=quiet)
            except Exception as e:
                log(f'  孤儿素材清理失败: {e}', quiet)
                total_fail += 1

        # 只跑了星魂/套装时不刷新周期，避免把图标更新周期一起延后
        if not args.dry_run and not skip_games:
            man = load_manifest()
            touch_last_check(man)
            save_manifest(man)

        log('更新完成。', quiet)
        return 1 if total_fail else 0
    finally:
        flush_log(quiet)


if __name__ == '__main__':
    code = main()
    if '--exit-code' in sys.argv:
        sys.exit(code)