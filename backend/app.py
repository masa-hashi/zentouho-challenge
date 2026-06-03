import os
import sqlite3
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
import secrets
from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Railway Volume は /data にマウントする想定。ローカルはカレントディレクトリ
_default_db = "/data/results.db" if os.path.isdir("/data") else "results.db"
DB_PATH       = os.getenv("DB_PATH", _default_db)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PORT          = int(os.getenv("PORT", 5000))
DEBUG         = os.getenv("DEBUG", "false").lower() == "true"


# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  TEXT    NOT NULL,
            nickname   TEXT    DEFAULT '',
            date       TEXT    NOT NULL,
            quiz_type  TEXT    NOT NULL,
            label      TEXT    NOT NULL,
            subject    TEXT    NOT NULL,
            score      INTEGER NOT NULL,
            total      INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

# gunicorn でも確実に初期化されるようモジュールロード時に実行
init_db()


# ─────────────────────────────────────────────
# Admin auth (session-based)
# ─────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        error = "パスワードが違います。"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ─────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────
@app.route("/api/result", methods=["POST"])
def save_result():
    data = request.get_json(silent=True) or {}
    required = ["deviceId", "date", "quizType", "label", "subject", "score", "total"]
    if not all(k in data for k in required):
        return jsonify({"error": "missing fields"}), 400

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO results
               (device_id, nickname, date, quiz_type, label, subject, score, total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["deviceId"],
                data.get("nickname", ""),
                data["date"],
                data["quizType"],
                data["label"],
                data["subject"],
                int(data["score"]),
                int(data["total"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────
# Admin dashboard
# ─────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM results ORDER BY created_at DESC"
        ).fetchall()]
    finally:
        conn.close()

    # ── per-user aggregation ──
    users_map: dict = {}
    for r in rows:
        did = r["device_id"]
        if did not in users_map:
            users_map[did] = {
                "device_id": did,
                "nickname": "",
                "results": [],
                "last_date": r["date"][:10],
            }
        if r["nickname"]:
            users_map[did]["nickname"] = r["nickname"]
        users_map[did]["results"].append(r)

    users = []
    for did, u in users_map.items():
        res = u["results"]
        total_score = sum(x["score"] for x in res)
        total_q     = sum(x["total"] for x in res)
        kok = [x for x in res if x["subject"] == "kokugo"]
        san = [x for x in res if x["subject"] == "sansu"]
        k_s = sum(x["score"] for x in kok); k_t = sum(x["total"] for x in kok)
        s_s = sum(x["score"] for x in san); s_t = sum(x["total"] for x in san)
        users.append({
            "device_id":   did,
            "nickname":    u["nickname"],
            "count":       len(res),
            "avg_pct":     round(total_score / max(total_q, 1) * 100),
            "kokugo_pct":  round(k_s / max(k_t, 1) * 100) if k_t else "-",
            "sansu_pct":   round(s_s / max(s_t, 1) * 100) if s_t else "-",
            "last_date":   u["last_date"],
        })
    users.sort(key=lambda x: x["count"], reverse=True)

    # ── global stats ──
    total_score_all = sum(r["score"] for r in rows)
    total_q_all     = sum(r["total"] for r in rows)
    overall_pct     = round(total_score_all / max(total_q_all, 1) * 100)
    today           = datetime.now().strftime("%Y-%m-%d")
    today_quizzes   = sum(1 for r in rows if r["date"][:10] == today)

    # ── per-category stats ──
    cat_map: dict = {}
    for r in rows:
        qt = r["quiz_type"]
        if qt not in cat_map:
            cat_map[qt] = {"label": r["label"], "subject": r["subject"],
                           "score": 0, "total": 0, "count": 0}
        cat_map[qt]["score"] += r["score"]
        cat_map[qt]["total"] += r["total"]
        cat_map[qt]["count"] += 1

    categories = [
        {
            "quiz_type": qt,
            "label":     c["label"],
            "subject":   c["subject"],
            "pct":       round(c["score"] / max(c["total"], 1) * 100),
            "count":     c["count"],
        }
        for qt, c in cat_map.items()
    ]
    categories.sort(key=lambda x: x["pct"])

    return render_template_string(
        ADMIN_HTML,
        total_users=len(users),
        total_quizzes=len(rows),
        overall_pct=overall_pct,
        today_quizzes=today_quizzes,
        users=users,
        categories=categories,
        recent=rows[:100],
    )


# ─────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>管理画面 ログイン</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Hiragino Kaku Gothic Pro','Meiryo',sans-serif;
         display:flex;justify-content:center;align-items:center;
         min-height:100vh;background:#F1F5F9}
    .card{background:#fff;padding:40px 36px;border-radius:20px;
          box-shadow:0 8px 32px rgba(0,0,0,.1);text-align:center;width:320px}
    h1{font-size:1.4rem;margin-bottom:6px}
    .sub{color:#6B7280;font-size:.85rem;margin-bottom:24px}
    .error{color:#EF4444;font-size:.85rem;margin-bottom:12px;
           background:#FEE2E2;padding:8px 12px;border-radius:8px}
    input{width:100%;padding:13px;border:2px solid #E5E7EB;border-radius:10px;
          font-size:1rem;outline:none;transition:border-color .15s;font-family:inherit}
    input:focus{border-color:#3B82F6}
    button{width:100%;margin-top:14px;padding:13px;background:#3B82F6;color:#fff;
           border:none;border-radius:10px;font-size:1rem;font-weight:bold;
           cursor:pointer;font-family:inherit}
    button:hover{background:#2563EB}
  </style>
</head>
<body>
<div class="card">
  <h1>🔐 管理画面</h1>
  <p class="sub">パスワードを入力してください</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="POST" action="/admin/login">
    <input type="password" name="password" placeholder="パスワード" autofocus>
    <button type="submit">ログイン</button>
  </form>
</div>
</body>
</html>"""


ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>管理ダッシュボード - 全統小チャレンジ</title>
  <style>
    :root{
      --kokugo:#E85D75; --sansu:#3B82F6; --accent:#F59E0B;
      --green:#10B981;  --red:#EF4444;   --gray:#6B7280;
      --bg:#F1F5F9;     --white:#fff;    --text:#1F2937;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Hiragino Kaku Gothic Pro','Meiryo',sans-serif;
         background:var(--bg);color:var(--text);min-height:100vh}

    /* header */
    header{
      background:linear-gradient(135deg,#1E3A5F,#2563EB);
      color:#fff;padding:16px 24px;
      display:flex;align-items:center;justify-content:space-between;
      position:sticky;top:0;z-index:100;
      box-shadow:0 3px 12px rgba(0,0,0,.25)
    }
    header h1{font-size:1.1rem;letter-spacing:.03em}
    .header-right{display:flex;gap:10px;align-items:center}
    .header-right a{
      padding:7px 14px;border-radius:8px;border:1px solid rgba(255,255,255,.4);
      color:#fff;text-decoration:none;font-size:.82rem;transition:background .15s
    }
    .header-right a:hover{background:rgba(255,255,255,.15)}

    main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}

    /* stats row */
    .stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
    .stat-card{
      background:var(--white);border-radius:16px;padding:20px 16px;text-align:center;
      box-shadow:0 2px 10px rgba(0,0,0,.06)
    }
    .stat-icon{font-size:1.8rem}
    .stat-val{font-size:2rem;font-weight:bold;margin:6px 0 4px}
    .stat-label{font-size:.75rem;color:var(--gray)}

    /* section */
    .section{background:var(--white);border-radius:18px;padding:24px;
             box-shadow:0 2px 10px rgba(0,0,0,.06);margin-bottom:24px}
    .section-head{
      display:flex;align-items:center;justify-content:space-between;margin-bottom:18px
    }
    .section-title{font-size:1.05rem;font-weight:bold}
    .search-box{
      padding:8px 12px;border:1.5px solid #E5E7EB;border-radius:9px;
      font-size:.85rem;outline:none;width:220px
    }
    .search-box:focus{border-color:var(--sansu)}

    /* table */
    .data-table{width:100%;border-collapse:collapse;font-size:.88rem}
    .data-table th{
      text-align:left;padding:10px 12px;
      background:#F8FAFC;color:var(--gray);font-weight:bold;
      border-bottom:2px solid #E5E7EB;white-space:nowrap
    }
    .data-table td{padding:11px 12px;border-bottom:1px solid #F3F4F6;vertical-align:middle}
    .data-table tbody tr:last-child td{border-bottom:none}
    .data-table tbody tr.user-row{cursor:pointer}
    .data-table tbody tr.user-row:hover td{background:#F8FAFC}
    .data-table tbody tr.detail-row td{padding:0;background:#F8FAFC}
    .detail-inner{
      padding:16px 20px;border-left:4px solid var(--sansu);
      display:none
    }
    .detail-inner.open{display:block}
    .detail-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-bottom:14px}
    .detail-stat{background:var(--white);border-radius:10px;padding:10px 14px;font-size:.83rem}
    .detail-stat .ds-val{font-size:1.2rem;font-weight:bold;color:var(--sansu)}
    .detail-stat .ds-lbl{color:var(--gray);font-size:.72rem;margin-top:2px}
    .detail-hist{width:100%;border-collapse:collapse;font-size:.82rem}
    .detail-hist th{padding:6px 10px;background:#EFF6FF;color:var(--sansu);text-align:left;font-weight:bold}
    .detail-hist td{padding:7px 10px;border-bottom:1px solid #E5E7EB}

    /* badges */
    .badge{
      display:inline-block;padding:3px 10px;border-radius:20px;font-size:.78rem;font-weight:bold
    }
    .badge-green{background:#D1FAE5;color:#065F46}
    .badge-yellow{background:#FEF3C7;color:#92400E}
    .badge-red{background:#FEE2E2;color:#991B1B}

    .subj-badge{
      display:inline-block;padding:3px 9px;border-radius:8px;font-size:.78rem;font-weight:bold
    }
    .subj-kokugo{background:#FEE2E9;color:var(--kokugo)}
    .subj-sansu{background:#DBEAFE;color:var(--sansu)}

    /* category bars */
    .cat-list{display:flex;flex-direction:column;gap:12px}
    .cat-row{display:grid;grid-template-columns:140px 1fr 50px 60px;align-items:center;gap:12px}
    .cat-label{font-size:.85rem;font-weight:bold;text-align:right}
    .cat-kokugo{color:var(--kokugo)}
    .cat-sansu{color:var(--sansu)}
    .bar-wrap{background:#F3F4F6;border-radius:8px;height:12px;overflow:hidden}
    .bar{height:100%;border-radius:8px;min-width:4px;transition:width .6s ease}
    .bar-kokugo{background:linear-gradient(90deg,#E85D75,#F997A8)}
    .bar-sansu{background:linear-gradient(90deg,#3B82F6,#60A5FA)}
    .cat-pct{font-size:.85rem;font-weight:bold;text-align:right}
    .cat-count{font-size:.72rem;color:var(--gray)}

    /* misc */
    .mono{font-family:monospace;font-size:.82rem}
    .text-gray{color:var(--gray)}
    .text-center{text-align:center}
    .empty{color:var(--gray);text-align:center;padding:32px;font-size:.93rem}
    .export-btn{
      padding:7px 14px;background:#F3F4F6;border:1.5px solid #E5E7EB;
      border-radius:9px;font-size:.82rem;cursor:pointer;font-family:inherit
    }
    .export-btn:hover{background:#E5E7EB}

    @media(max-width:700px){
      .stats-row{grid-template-columns:repeat(2,1fr)}
      .cat-row{grid-template-columns:100px 1fr 40px 50px}
    }
    @media(max-width:480px){
      .stats-row{grid-template-columns:1fr 1fr}
      .cat-row{grid-template-columns:80px 1fr 38px}
      .cat-count{display:none}
    }
  </style>
</head>
<body>

<header>
  <h1>🎓 全統小チャレンジ　管理ダッシュボード</h1>
  <div class="header-right">
    <a href="/admin">↻ 更新</a>
    <a href="/admin/logout">🔓 ログアウト</a>
  </div>
</header>

<main>

  <!-- Stats -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-icon">👤</div>
      <div class="stat-val">{{ total_users }}</div>
      <div class="stat-label">ユーザー数</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">📊</div>
      <div class="stat-val">{{ total_quizzes }}</div>
      <div class="stat-label">チャレンジ総数</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">✅</div>
      <div class="stat-val">{{ overall_pct }}%</div>
      <div class="stat-label">全体正解率</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon">📅</div>
      <div class="stat-val">{{ today_quizzes }}</div>
      <div class="stat-label">本日のチャレンジ</div>
    </div>
  </div>

  <!-- Users -->
  <div class="section">
    <div class="section-head">
      <span class="section-title">👤 ユーザー一覧</span>
      <div style="display:flex;gap:10px;align-items:center">
        <input class="search-box" id="user-search" placeholder="🔍 名前・IDで検索" oninput="filterUsers()">
        <button class="export-btn" onclick="exportCSV()">📥 CSV</button>
      </div>
    </div>
    {% if users %}
    <div style="overflow-x:auto">
    <table class="data-table" id="user-table">
      <thead>
        <tr>
          <th>ニックネーム</th>
          <th>デバイスID</th>
          <th>チャレンジ数</th>
          <th>正かいりつ</th>
          <th>国語</th>
          <th>算数</th>
          <th>最終アクセス</th>
        </tr>
      </thead>
      <tbody>
        {% for u in users %}
        <tr class="user-row" data-did="{{ u.device_id }}"
            data-nick="{{ u.nickname }}"
            onclick="toggleDetail('{{ u.device_id }}')">
          <td><strong>{{ u.nickname if u.nickname else '（未設定）' }}</strong></td>
          <td class="mono text-gray">{{ u.device_id[:12] }}…</td>
          <td class="text-center">{{ u.count }}回</td>
          <td class="text-center">
            <span class="badge
              {% if u.avg_pct >= 80 %}badge-green
              {% elif u.avg_pct >= 60 %}badge-yellow
              {% else %}badge-red{% endif %}">
              {{ u.avg_pct }}%
            </span>
          </td>
          <td class="text-center">
            {% if u.kokugo_pct != '-' %}
            <span class="badge
              {% if u.kokugo_pct >= 80 %}badge-green
              {% elif u.kokugo_pct >= 60 %}badge-yellow
              {% else %}badge-red{% endif %}">
              {{ u.kokugo_pct }}%
            </span>
            {% else %}<span class="text-gray">—</span>{% endif %}
          </td>
          <td class="text-center">
            {% if u.sansu_pct != '-' %}
            <span class="badge
              {% if u.sansu_pct >= 80 %}badge-green
              {% elif u.sansu_pct >= 60 %}badge-yellow
              {% else %}badge-red{% endif %}">
              {{ u.sansu_pct }}%
            </span>
            {% else %}<span class="text-gray">—</span>{% endif %}
          </td>
          <td class="text-gray">{{ u.last_date }}</td>
        </tr>
        <tr class="detail-row" id="detail-{{ u.device_id }}" style="display:none">
          <td colspan="7">
            <div class="detail-inner" id="detail-inner-{{ u.device_id }}">
              <!-- filled by JS -->
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    {% else %}
    <p class="empty">データがありません。</p>
    {% endif %}
  </div>

  <!-- Category breakdown -->
  {% if categories %}
  <div class="section">
    <div class="section-head">
      <span class="section-title">📊 カテゴリ別正解率</span>
    </div>
    <div class="cat-list">
      {% for c in categories|reverse %}
      <div class="cat-row">
        <span class="cat-label {{ 'cat-kokugo' if c.subject == 'kokugo' else 'cat-sansu' }}">
          {{ c.label }}
        </span>
        <div class="bar-wrap">
          <div class="bar {{ 'bar-kokugo' if c.subject == 'kokugo' else 'bar-sansu' }}"
               style="width:{{ c.pct }}%"></div>
        </div>
        <span class="cat-pct">{{ c.pct }}%</span>
        <span class="cat-count text-gray">{{ c.count }}回</span>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <!-- Recent activity -->
  <div class="section">
    <div class="section-head">
      <span class="section-title">🕐 最近の学習履歴（最新100件）</span>
    </div>
    {% if recent %}
    <div style="overflow-x:auto">
    <table class="data-table">
      <thead>
        <tr>
          <th>日時</th>
          <th>ニックネーム</th>
          <th>カテゴリ</th>
          <th>スコア</th>
          <th>正かいりつ</th>
        </tr>
      </thead>
      <tbody>
        {% for r in recent %}
        {% set pct = (r.score / r.total * 100)|int %}
        <tr>
          <td class="mono text-gray">{{ r.date[:16]|replace('T',' ') }}</td>
          <td>{{ r.nickname if r.nickname else '（未設定）' }}</td>
          <td>
            <span class="subj-badge {{ 'subj-kokugo' if r.subject == 'kokugo' else 'subj-sansu' }}">
              {{ r.label }}
            </span>
          </td>
          <td class="text-center">{{ r.score }}/{{ r.total }}</td>
          <td class="text-center">
            <span class="badge
              {% if pct >= 80 %}badge-green
              {% elif pct >= 60 %}badge-yellow
              {% else %}badge-red{% endif %}">
              {{ pct }}%
            </span>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    {% else %}
    <p class="empty">データがありません。</p>
    {% endif %}
  </div>

</main>

<script>
// ── all results data (for detail view) ──
const ALL_RESULTS = {{ recent | tojson }};

function pctBadge(pct){
  const cls = pct>=80?'badge-green':pct>=60?'badge-yellow':'badge-red';
  return `<span class="badge ${cls}">${pct}%</span>`;
}

function toggleDetail(did){
  const row   = document.getElementById('detail-' + did);
  const inner = document.getElementById('detail-inner-' + did);
  if(!row) return;
  const isOpen = inner.classList.contains('open');
  // close all others
  document.querySelectorAll('.detail-inner').forEach(el => el.classList.remove('open'));
  document.querySelectorAll('.detail-row').forEach(el => el.style.display = 'none');
  if(isOpen) return;

  // gather this user's results
  const userRes = ALL_RESULTS.filter(r => r.device_id === did);
  if(!userRes.length){ inner.innerHTML='<p class="text-gray">データがありません</p>'; }
  else {
    const totalS = userRes.reduce((s,r)=>s+r.score,0);
    const totalT = userRes.reduce((s,r)=>s+r.total,0);
    const kok = userRes.filter(r=>r.subject==='kokugo');
    const san = userRes.filter(r=>r.subject==='sansu');
    const kS=kok.reduce((s,r)=>s+r.score,0), kT=kok.reduce((s,r)=>s+r.total,0);
    const sS=san.reduce((s,r)=>s+r.score,0), sT=san.reduce((s,r)=>s+r.total,0);
    const avg = Math.round(totalS/Math.max(totalT,1)*100);
    const kPct= kT? Math.round(kS/kT*100) : null;
    const sPct= sT? Math.round(sS/sT*100) : null;

    let html = `<div class="detail-grid">
      <div class="detail-stat"><div class="ds-val">${userRes.length}回</div><div class="ds-lbl">チャレンジ数</div></div>
      <div class="detail-stat"><div class="ds-val">${avg}%</div><div class="ds-lbl">全体正解率</div></div>
      ${kPct!==null?`<div class="detail-stat"><div class="ds-val">${kPct}%</div><div class="ds-lbl">国語正解率</div></div>`:''}
      ${sPct!==null?`<div class="detail-stat"><div class="ds-val">${sPct}%</div><div class="ds-lbl">算数正解率</div></div>`:''}
    </div>`;
    html += `<table class="detail-hist"><thead><tr>
      <th>日時</th><th>カテゴリ</th><th>スコア</th><th>正かいりつ</th>
    </tr></thead><tbody>`;
    userRes.slice(0, 20).forEach(r => {
      const p = Math.round(r.score/r.total*100);
      const cls = r.subject==='kokugo'?'subj-kokugo':'subj-sansu';
      html += `<tr>
        <td class="mono text-gray">${r.date.slice(0,16).replace('T',' ')}</td>
        <td><span class="subj-badge ${cls}">${r.label}</span></td>
        <td class="text-center">${r.score}/${r.total}</td>
        <td class="text-center">${pctBadge(p)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    if(userRes.length > 20) html += `<p class="text-gray" style="margin-top:8px;font-size:.78rem">…他 ${userRes.length-20} 件</p>`;
    inner.innerHTML = html;
  }
  row.style.display = '';
  inner.classList.add('open');
}

// ── search/filter ──
function filterUsers(){
  const q = document.getElementById('user-search').value.toLowerCase();
  document.querySelectorAll('#user-table tbody .user-row').forEach(tr => {
    const nick = tr.dataset.nick.toLowerCase();
    const did  = tr.dataset.did.toLowerCase();
    const show = !q || nick.includes(q) || did.includes(q);
    tr.style.display = show ? '' : 'none';
    const detail = document.getElementById('detail-' + tr.dataset.did);
    if(detail) detail.style.display = 'none';
  });
}

// ── CSV export ──
function exportCSV(){
  const rows = {{ recent | tojson }};
  if(!rows.length){ alert('データがありません'); return; }
  const header = ['日時','デバイスID','ニックネーム','カテゴリ','科目','スコア','合計','正かいりつ(%)'];
  const lines  = [header.join(',')];
  rows.forEach(r => {
    const pct = Math.round(r.score/r.total*100);
    lines.push([
      r.date.slice(0,16).replace('T',' '),
      r.device_id,
      r.nickname || '',
      r.label,
      r.subject === 'kokugo' ? '国語' : '算数',
      r.score, r.total, pct
    ].map(v => `"${String(v).replace(/"/g,'""')}"`).join(','));
  });
  const blob = new Blob(['﻿'+lines.join('\\n')], {type:'text/csv;charset=utf-8;'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'zentouho_results_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"🚀  http://localhost:{PORT}")
    print(f"🔐  管理画面: http://localhost:{PORT}/admin")
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
