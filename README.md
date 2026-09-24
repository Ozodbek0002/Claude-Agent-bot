# Telegram → Claude Code bot (Windows)

Telegram'dan promt yozasiz, bot uni kompyuteringizdagi tanlangan loyiha papkasida **Claude Code** orqali bajaradi va natijani qaytaradi. Terminal ochish shart emas.

```
Telegram  ──►  bot.py (kompyuteringizda)  ──►  claude -p  (C:\...\projects\<loyiha>)
   ▲                                                     │
   └──────────── jonli holat + yakuniy javob ◄───────────┘
```

## 1. Bir martalik tayyorgarlik

1. **Git for Windows** o'rnating (Claude Code terminal buyruqlari uchun Git Bash'dan foydalanadi): https://git-scm.com/download/win
2. **Claude Code** o'rnating (PowerShell):
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   So'ng istalgan terminalda bir marta `claude` deb yozib, hisobingizga kiring. Keyin yopib qo'yishingiz mumkin.
3. **Python 3.11+** o'rnating (o'rnatishda "Add to PATH" belgilang).
4. **Telegram bot yarating**: @BotFather → `/newbot` → tokenni nusxalang.

## 2. Sozlash

1. Shu papkani istalgan joyga qo'ying (masalan `C:\tools\tg-claude-bot`).
2. `.env.example` ni `.env` deb nusxalang va to'ldiring:
   - `TELEGRAM_BOT_TOKEN` — BotFather tokeni
   - `PROJECTS_DIR` — loyihalaringiz papkasi, masalan `C:\Users\Diyorbek\projects`
   - `ALLOWED_USER_IDS` — hozircha **bo'sh qoldiring**
3. `start_bot.bat` ni ikki marta bosing. Birinchi safar kutubxonalar o'rnatiladi.
4. Botga Telegram'da istalgan xabar yozing — u sizga **Telegram ID**'ingizni aytadi.
5. O'sha ID'ni `.env` dagi `ALLOWED_USER_IDS=` ga yozing, oynani yopib `start_bot.bat` ni qayta ishga tushiring. Endi bot faqat sizga javob beradi.

## 3. Foydalanish

| Buyruq | Vazifasi |
|---|---|
| `/projects` | Loyihalar ro'yxati (tugmalar bilan tanlaysiz). Ichida faqat papkalar bo'lgan «guruh» papkalar (masalan `Bots`) ochiladi: `Bots/TelegramDavomat` |
| *oddiy matn* | Tanlangan loyihada Claude Code'ga promt |
| `@loyiha promt` | Almashtirmasdan boshqa loyihaga bir martalik promt |
| `/sessions` | Shu loyihaning oxirgi 10 ta sessiyasi (terminalda ochilganlari ham) — tanlab davom ettirish yoki yangisini boshlash |
| `/new` | Shu loyihada yangi suhbat (aks holda oldingi suhbat davom etadi) |
| `/stop` | Ishlayotgan vazifani to'xtatish |
| `/status` | Loyiha, rejim, model, ishlayotgan vazifalar |
| `/mode` | Ruxsat rejimi: `plan` / `edit` / `auto` / `full` |
| `/model sonnet` | Model tanlash (`/model default` — standart) |
| `/diff` | `git status` + `git diff --stat` |
| 📎 rasm/fayl | Loyihadagi `.tg_uploads/` ga saqlanadi va Claude'ga beriladi (masalan xato skrinshoti) |

### Loyihani ishga tushirish va skrinshot

| Buyruq | Vazifasi |
|---|---|
| `/run` | Tanlangan loyihani fonda ishga tushiradi (`npm run dev`/`npm start`, `ng serve`, `mvn spring-boot:run`, `gradlew bootRun`… o'zi aniqlaydi). O'zingiz ham berishingiz mumkin: `/run npm run dev -- --port 4300` |
| `/shot` | Ishlayotgan saytning brauzer skrinshoti. `/shot /editor` — boshqa sahifa, `/shot full` — butun sahifa, `/shot mobile` — telefon ko'rinishi, `/shot http://localhost:8080` — istalgan URL |
| `/runlog` | Server chiqishining oxirgi 40 qatori |
| `/stoprun` | Serverni to'xtatish |

Claude'dan ham so'rash mumkin: *«editor sahifasini ochib, mobil va desktop skrinshot yubor»*. Claude skrinshotni loyihadagi `.tg_outbox/` papkasiga saqlaydi, bot esa uni avtomatik Telegram'ga yuboradi. Buning uchun `full` rejim kerak (`/mode full`), chunki skrinshot terminal buyrug'i orqali olinadi.

Skrinshotlar kompyuterdagi **Microsoft Edge** (yoki Chrome) orqali olinadi, qo'shimcha brauzer yuklab olinmaydi.

Ish davomida bot bitta xabarni jonli yangilab turadi: qaysi fayl o'qildi/tahrirlandi, qaysi buyruq ishga tushdi. Oxirida Claude javobini yuboradi. Turli loyihalarda bir vaqtda ishlatsa bo'ladi; bitta loyihada esa bir vaqtda bitta vazifa.

### Ruxsat rejimlari

- **plan** — faqat o'qiydi va reja tuzadi, hech narsani o'zgartirmaydi.
- **edit** (standart) — fayllarni tahrirlaydi, lekin terminal buyruqlari (`mvn test`, `git push`…) rad etiladi. Rad etilgan amallarni bot alohida ko'rsatadi.
- **auto** — xavfsiz amallarni o'zi tasdiqlaydi (hisobingizda mavjud bo'lsa).
- **full** — hammasiga ruxsat. Qulay, lekin Claude istalgan buyruqni so'ramasdan bajaradi.

Oraliq variant: `.env` da `CLAUDE_ALLOWED_TOOLS=Bash(git:*) Bash(mvn:*)` — `edit` rejimida ham faqat shu buyruqlarga ruxsat beriladi.

💡 Har bir loyihaga `CLAUDE.md` fayl qo'ysangiz (build/test buyruqlari, arxitektura qoidalari), Claude uni har safar avtomatik o'qiydi.

## 4. Kompyuter yoqilganda avtomatik ishga tushirish

1. `Win + R` → `shell:startup` → Enter.
2. Ochilgan papkaga `start_hidden.vbs` ning **yorlig'ini** (shortcut) qo'ying.

Endi bot kompyuter yoqilganda oynasiz ishga tushadi. Loglar: `bot.log`.

⚠️ Kompyuter uxlab qolsa bot ham ishlamaydi: *Settings → System → Power → Sleep → Never* (hech bo'lmasa zaryadda).

## 5. Xavfsizlik

- Bot faqat `ALLOWED_USER_IDS` dagi foydalanuvchiga javob beradi; boshqalar `bot.log` ga yoziladi.
- `.env` (token) ni hech kimga bermang va git'ga qo'shmang.
- Telegram hisobingiz = kompyuteringiz kaliti. Telegram'da **ikki bosqichli parol** (Settings → Privacy → Two-Step Verification) albatta yoqing, ayniqsa `full` rejimdan foydalansangiz.
