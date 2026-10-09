# Grid++ 素材庫平台

供 Grid++ 遊戲素材上傳、搜尋、收藏、編組與匯出的 Web 平台。後端提供 REST API，並直接託管同一套原生 HTML/CSS/JavaScript 前端。

核心功能：

- 帳號註冊、登入、登出與個人資料管理
- 32×32 RGBA8888 素材上傳、預覽、搜尋與刪除
- 私人收藏資料夾
- 素材包編排與 Grid++ `assets.db` 匯出
- OpenAPI 文件與一致的 API 錯誤格式

## 技術組成

- Python 3.13
- FastAPI、Pydantic、Uvicorn
- SQLAlchemy、Alembic
- SQLite（預設開發資料庫）
- Pillow（圖片處理）
- pytest、HTTPX（測試）

## 專案結構

```text
app/
├── main.py              # FastAPI 應用程式與路由
├── models.py            # SQLAlchemy 資料模型
├── schemas.py           # API 回應模型
├── validation.py        # 請求驗證與正規化
├── image_processing.py  # 32×32 RGBA 圖片處理
├── security.py          # 密碼雜湊與 JWT
├── config.py            # 環境設定
├── database.py          # Engine、Session 與 FastAPI dependency
└── static/              # 內建前端
alembic/
├── env.py
└── versions/            # 資料庫遷移
docker/
├── entrypoint.sh        # 容器啟動：遷移後跑 uvicorn
└── nginx.conf           # 內層 nginx：靜態檔與 API 反代
tests/                   # API、資料庫與圖片處理測試
Dockerfile
docker-compose.yml
docker-compose.edge.yml  # 可選：讓前端 nginx 加入既有外部網路
```

## 本機開發

### 1. 建立環境

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
cp .env.example .env
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

### 2. 設定環境變數

產生開發用 JWT secret：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

將結果填入 `.env` 的 `JWT_SECRET`。可用設定如下：

- `JWT_SECRET`：必填，至少 32 bytes；用於簽署一小時有效的 access token。
- `DATABASE_URL`：選填；預設為專案根目錄的 `grid_platform.db`。
- `CORS_ORIGINS`：選填；外部前端的允許來源，以逗號分隔，不接受 `*`。使用內建前端時不需設定。

行程環境變數的優先度高於 `.env`，部署環境應直接由平台注入設定。

### 3. 建立資料庫並啟動

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

- Web UI：<http://127.0.0.1:8000/>
- OpenAPI：<http://127.0.0.1:8000/docs>

## 開發指令

執行全部測試：

```bash
pytest
```

同步遠端變更後：

- `pyproject.toml` 有更新：執行 `python -m pip install -e ".[test]"`。
- `alembic/versions/` 有新 revision：執行 `alembic upgrade head`。

## API 與資料格式

需要登入的 API 使用 `Authorization: Bearer <token>`。Access token 有效期為一小時，登出後會立即撤銷。

素材在資料庫與 API 中皆使用固定 4096 bytes 的 32×32 RGBA8888 原始資料。`GET /sprites/{id}/image` 回傳 `application/octet-stream`，不是 PNG。

圖片上傳提供三種處理模式：

- `pixel`：偵測並保留像素網格，必要時裁切。
- `fit`：以 nearest-neighbor 將完整內容等比例縮入 32×32。
- `smooth`：以預乘 Alpha 的高品質取樣縮放一般插圖。

素材包由 `GET /packs/{id}/export` 匯出為 SQLite。檔案只包含 `sprites(id, name, tags, image_data)`，可直接供 Grid++ 引擎使用。

完整端點、請求欄位與回應格式以 `/docs` 產生的 OpenAPI 文件為準。

## 資料庫遷移

Schema 變更流程：

1. 修改 `app/models.py` 及相關應用程式碼。
2. 建立新 revision：`alembic revision -m "簡短說明"`；需要時加上 `--autogenerate`。
3. 人工檢查 upgrade 與 downgrade 內容。
4. 執行 `alembic upgrade head` 並補上相應測試。

遷移規範：

- 不修改已提交、可能已套用的 revision，也不更動既有 `revision` 或 `down_revision` ID。
- 每個獨立 schema 變更建立新的 revision。
- 不手動修改 `alembic_version`。
- 不以修改 `alembic.ini` 切換資料庫；使用 `DATABASE_URL`。

## Docker 部署

若從舊版更新，先在本專案目錄執行 `docker compose -p grid-platform down`，再依下列步驟啟動。Compose 固定沿用舊版 SQLite volume 名稱；切換時不得使用 `down -v`。

本節是伺服器部署 SOP。Compose 內含前端 nginx、FastAPI 與 SQLite；前端 nginx 提供 `app/static/`，其餘請求反代到 FastAPI。SQLite 資料存放在 Docker volume，應用程式啟動時會自動執行 `alembic upgrade head`。

Compose 預設不對宿主機發布 port。若要讓外部使用者連線，必須依下列步驟接到伺服器既有的外層 nginx。

### 一、部署前確認

1. 進入專案根目錄。
2. 確認主機已安裝 Docker，且 `docker compose version` 可正常執行。
3. 若要對外提供服務，確認外層 nginx：
   - 以 container 執行。
   - 已加入一張既有的 Docker network。
   - 可修改其主機專用 nginx 設定。
4. 記下該 Docker network 名稱；以下範例使用 `nginx`。

可用下列指令確認 network 是否存在：

```bash
docker network inspect nginx
```

### 二、建立環境設定

1. 首次部署時複製範例檔：

   ```bash
   cp .env.example .env
   ```

   若 `.env` 已存在，不要再次複製，以免覆蓋既有設定。

2. 產生 JWT secret：

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. 編輯 `.env`，將產生的值填入 `JWT_SECRET`，不要將 `.env` 提交到版本庫。

4. 若只需要讓容器彼此連線，不需修改其他設定，可直接進行「四、啟動服務」。此模式不發布宿主機 port，因此無法從宿主機或外部網路直接連線。

5. 若要接到外層 nginx，在 `.env` 取消註解並填入：

   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.edge.yml
   EDGE_NETWORK=nginx
   ```

   將 `nginx` 改成第一節確認的實際 network 名稱。

### 三、設定外層 nginx

1. 確認外層 nginx container 也在 `EDGE_NETWORK` 指定的 network 上。
2. 在外層 nginx 的主機專用設定加入下列內容；不要將該主機專用設定放入本 repo：

   ```nginx
   client_max_body_size 6m;

   location / {
       proxy_pass http://gridplusplus:80;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

3. 檢查 nginx 設定語法，確認成功後再依伺服器原有方式 reload nginx。

`gridplusplus` 是 Compose 專案名稱與此服務在外部 network 上的固定別名，對應 `https://gridplusplus.ntuee.org/`。

### 四、啟動服務

1. 先檢查 Compose 設定：

   ```bash
   docker compose config --quiet
   ```

2. 建置並在背景啟動：

   ```bash
   docker compose up -d --build
   ```

3. 確認 `nginx` 與 `app` 皆為 `healthy`：

   ```bash
   docker compose ps
   ```

4. 若服務未正常啟動，先查看近期 log：

   ```bash
   docker compose logs --tail=100 app nginx
   ```

### 五、部署驗收

1. 從服務內部檢查健康端點：

   ```bash
   docker compose exec nginx wget -qO- http://127.0.0.1/health
   ```

2. 已設定外層 nginx 時，再以實際網域開啟首頁與 `/docs`。
3. 建立第一個使用者帳號：
   - 開啟首頁的「帳號」。
   - 在登入畫面選擇「點此註冊」。
   - 填入公開使用者名稱、Email，以及 8 至 128 個字元的密碼。
   - 按下「建立帳號並登入」。
4. 登出後以相同 Email 與密碼重新登入，並至少讀取一次既有資料。

部署程序不會自動建立預設帳號或管理員。現行 `/auth/register` 是公開註冊端點，所有可連到網站的訪客都能建立一般帳號；系統目前沒有管理員角色。

### 六、更新或停止服務

更新程式碼後，重新建置並啟動；啟動時會自動套用尚未執行的資料庫遷移：

```bash
docker compose up -d --build
docker compose ps
```

停止並移除 container，但保留 SQLite volume：

```bash
docker compose down
```

只有確定要連同資料庫永久刪除時，才可執行：

```bash
docker compose down -v
```

`docker compose down` 不會刪除外部 nginx network；`docker compose down -v` 會刪除本專案的 SQLite volume 與其中資料。
