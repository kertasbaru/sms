# SMS Cloud - Server Pemulihan Akun Sekolah

Server backend untuk pemulihan akun sekolah menggunakan SQLiteCloud. Aplikasi ini menyediakan API untuk memverifikasi, memulihkan, dan mentransfer data sekolah beserta akun admin dari database cloud ke database lokal.

## Fitur Utama

- **Pengecekan Sekolah** – Memverifikasi apakah sekolah terdaftar di database cloud.
- **Verifikasi Pemulihan** – Memastikan data sekolah (nama, email, kontak) sesuai sebelum proses pemulihan.
- **Pemulihan Akun** – Mengunduh data sekolah dan admin dari cloud, menyimpannya ke database lokal.
- **Enkripsi Data** – Data pemulihan dienkripsi menggunakan Fernet (AES) untuk keamanan transfer.
- **Transfer Otomatis** – Mengirim data pemulihan langsung ke aplikasi utama melalui API.

## Prasyarat

- Python 3.9 atau lebih baru
- Koneksi internet (untuk akses SQLiteCloud)
- Variabel environment `SQLITECLOUD_CONNECTION_STRING` berisi connection string ke database SQLiteCloud

## Instalasi

1. **Clone repositori ini:**

   ```bash
   git clone https://github.com/kertasbaru/sms.git
   cd sms
   ```

2. **Buat virtual environment (opsional tapi disarankan):**

   ```bash
   python -m venv venv
   source venv/bin/activate    # Linux/macOS
   venv\Scripts\activate       # Windows
   ```

3. **Instal dependensi:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Atur variabel environment:**

   Buat file `.env` atau ekspor variabel secara langsung:

   ```bash
   export SQLITECLOUD_CONNECTION_STRING="sqlitecloud://<host>:<port>/<database>?apikey=<api_key>"
   ```

   Ganti `<host>`, `<port>`, `<database>`, dan `<api_key>` sesuai konfigurasi SQLiteCloud Anda.

## Menjalankan Server

Jalankan server pemulihan menggunakan salah satu cara berikut:

**Cara 1 – Langsung:**

```bash
python cloud_backend.py
```

**Cara 2 – Melalui script starter:**

```bash
python smscloud_server.py
```

Server akan berjalan di `http://localhost:8001` secara default.

## Endpoint API

| Metode | Endpoint                          | Deskripsi                                  |
|--------|-----------------------------------|--------------------------------------------|
| GET    | `/`                               | Status server                              |
| GET    | `/health`                         | Pengecekan kesehatan server                |
| POST   | `/check-school`                   | Cek apakah sekolah terdaftar di cloud      |
| POST   | `/verify-recovery`                | Verifikasi detail pemulihan sekolah        |
| POST   | `/perform-recovery`               | Lakukan proses pemulihan lengkap           |
| GET    | `/recovery-status`                | Lihat status pemulihan saat ini            |
| GET    | `/recovery/blob/{email}`          | Dapatkan blob pemulihan terenkripsi        |
| POST   | `/recovery/import-blob`           | Impor blob pemulihan ke aplikasi utama     |
| POST   | `/recovery/auto-import/{email}`   | Pemulihan otomatis dan impor sekaligus     |
| POST   | `/transfer-to-main`               | Transfer data ke database utama            |

## Struktur Proyek

```
sms/
├── cloud_backend.py       # Server utama (FastAPI)
├── smscloud_server.py     # Script untuk memulai server
├── database/
│   └── cloud_db.py        # Klien SQLiteCloud
├── requirements.txt       # Daftar dependensi Python
├── .gitignore
└── README.md
```

## Konfigurasi

Variabel environment yang dapat diatur:

| Variabel                        | Deskripsi                              | Default           |
|---------------------------------|----------------------------------------|-------------------|
| `SQLITECLOUD_CONNECTION_STRING` | Connection string SQLiteCloud          | *(wajib diisi)*   |
| `RECOVERY_SECRET`              | Kunci rahasia untuk enkripsi pemulihan  | `CHANGE_ME_IN_PRODUCTION` |
| `PORT`                         | Port server                             | `8001`            |
| `HOST`                         | Host server                             | `0.0.0.0`         |

> **Penting:** Pastikan `RECOVERY_SECRET` diubah dari nilai default saat digunakan di lingkungan produksi.
