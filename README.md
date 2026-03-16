# SMS Cloud Recovery Server

Panduan singkat instalasi dan menjalankan project ini di **Termux**.

## Prasyarat

- Aplikasi Termux terbaru
- Koneksi internet

## Instalasi di Termux

```bash
pkg update && pkg upgrade -y
pkg install python git -y
```

Clone repository:

```bash
git clone https://github.com/kertasbaru/sms.git
cd sms
```

Install dependency Python:

```bash
pip install --upgrade pip
pip install fastapi uvicorn requests cryptography pydantic
```

## Cara Menjalankan

Jalankan server dengan:

```bash
python cloud_backend.py
```

Secara default server berjalan di:

- `http://127.0.0.1:8001`
- `http://localhost:8001`

Untuk memastikan server aktif, buka:

- `http://127.0.0.1:8001/health`

## Catatan

- Database lokal akan dibuat otomatis di folder `database/`.
- Jika port `8001` bentrok, set port lain:

```bash
PORT=9000 python cloud_backend.py
```
