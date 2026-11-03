# 米哈游养成记录 (mihoyo-tracker)

记录原神 / 崩坏：星穹铁道 / 绝区零 三个游戏的养成进度：角色、武器、遗器、星魂，本地存档、离线可查。

> 本仓库只包含配套工具脚本与文档，主程序为 PyInstaller 打包的免安装版，见 Releases。

## 包含内容

| 路径 | 说明 |
|---|---|
| `tools/update_assets.py` | 米游社观测枢素材月度更新器，抓角色/武器/遗器图标与套装效果 |
| `records.example.json` | 存档空结构样例 |

## 素材更新器

```bash
python tools/update_assets.py
```

从米游社官方 wiki 增量拉取素材到 `assets_wiki/`，并刷新名字映射表与套装效果库。
素材版权归米哈游所有，本项目仅做本地缓存，不重新分发。

## 存档结构

```json
{ "version": 1, "games": { "<game_key>": { "roles": [], "weapons": [] } } }
```

## 成品

见 Releases 页的 `mihoyo-tracker-win-x64.zip`，解压双击即用。
