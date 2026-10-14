# BIST Pay Piyasası tatil takvimi taslağı (2023-01-01 .. 2027-03-31)

Durum: `draft_unverified` (`verified_by: null`). Dosya: `configs/market/bist_holidays_draft.json`.
Araştırma tarihi: 2026-10-14 (UTC erişim zamanları JSON `sources` içinde). Koda bağlanmadı; Efe resmi listeyle doğrulayana kadar kullanılmamalı.

## Yıl yıl bulgular

| Yıl | Kaynak türü | Tam kapalı (hafta içi / hafta sonu) | Yarım gün | Not |
|---|---|---|---|---|
| 2023 | Resmi BIST Pay Piyasası PDF + duyuru (07.12.2022) | 7 / 7 (+5 deprem, aşağıda) | 2 (20.04, 27.06) | 28.10.2023 çelişkili (aşağıda) |
| 2024 | Resmi BIST Pay Piyasası PDF (duyuru PDF'i bulunamadı) | 12 / 3 | 2 (09.04, 28.10) | |
| 2025 | Resmi BIST Pay Piyasası PDF + duyuru 31449 (24.12.2024) | 10 / 5 | 2 (05.06, 28.10) | |
| 2026 | Resmi BIST Pay Piyasası PDF + duyuru 41625 (04.12.2025) | 10 / 4 | 3 (19.03, 26.05, 28.10) | |
| 2027 Q1 | Resmi BIST kaynağı YOK; Diyanet 2027 resmi tatil listesi | 3 (09-11.03) + 1 (01.01) | 1 (08.03) | hepsi `expected_not_announced` |

Sayılar JSON'daki `days` ile aynıdır (hafta sonu günleri resmi tablolarda geçtiği için dahil, `weekday` alanı var).
Yarım gün bitişi (13:00): PDF'ler yalnız "yarım gün seans" der; saat resmi "Resmi Tatil Günleri" sayfasından (`/en/official-holidays`, "Until 13:00"). Bu sayfa tüm piyasalar için tek tablo; Pay Piyasası'na özgü sütun yok.

Resmi tablolar, 2023-2026 için her yıl BIST'in "Pay Piyasası YYYY Yılı Tatil Tablosu" PDF'iyle sayfa tablosunun birbirini tuttuğunu gösterdi; İdari izin köprü günü olarak BIST'in kapalı ilan ettiği hiçbir gün görülmedi (eklenmedi).

## Belirsizlikler / boşluklar (Efe kontrol etsin)

1. **Deprem kapanışı, 2023-02-08 .. 2023-02-14 (5 iş günü)**: resmi tatil değil, olağanüstü kapanış. 08.02 gün içinde işlem görüp 11:00'de durduruldu ve o günkü tüm işlemler iptal edildi; 09, 10, 13, 14.02 kapalı; yeniden açılış 15.02. Bunu yalnız aracı kurum kopyalarından (Gedik, QNB) ve haberlerden (arama sonucu metni) gördüm; borsaistanbul.com/KAP orijinal metni açılamadı. Hepsi `status: "uncertain"`. Resmi KAP bildirimini doğrulayın.
2. **2023-10-28 (Cumartesi)**: PDF'te "(Yarım gün)" etiketi, ama aynı PDF "28 ve 29 Ekim seans yapılmayacaktır" der; resmi sayfa "Until 13:00". Cumartesi olduğundan fiyat verisini etkilemez; `uncertain`.
3. **Hafta sonuna denk gelen arifeler** (2024-06-15, 2025-03-29): PDF "Arefe-Yarım gün" yazar, ama seans yok listesinde ve sayfada "Closed"; `full_closure` olarak işaretlendi (hafta sonu, etkisiz).
4. **2024 duyuru PDF'i** (Genel Müdür Yardımcısı imzalı yıllık duyuru) bulunamadı; 2024 yalnız Pay Piyasası tablosu PDF'ine ve sayfaya dayanır.
5. **2027 Q1**: BIST'in 2027 tablosu henüz yayımlanmamış/bulunamadı (BIST 2026 için 04.12.2025'te yayımlamıştı; 2027 tablosu muhtemelen Aralık 2026). Diyanet listesi (2429 sayılı Kanun'a göre): 01.01 Cuma; arefe 08.03 Pazartesi (1/2 gün); 09-11.03 bayram. Arefe seans bitişi (13:00 olacağı varsayımı yalnız geçmiş yıllardan) JSON'da `null`. Ay görünümüne göre bayram tarihi ve olası idari izin uzatması resmi duyuruyla değişebilir.
6. Tek hisse işlem durdurmaları, devre kesici, teknik arıza kaynaklı kısa seans kesintileri kapsam dışı; her gün için seans kayıtlarıyla ayrıca doğrulanmalı.
7. Tarayıcı aracı gerekmedi; borsaistanbul.com sayfaları doğrudan indirildi. Sayfa özetini alan araç bir kez "socket hang up" verdi; PDF'ler `pdftotext` ile okundu.

## Yahoo fiyat geçmişi bulgularıyla çapraz kontrol (yalnız tarih düzeyi)

Yahoo'nun düşürdüğü/yer tutucu satırlar ve taslaktaki karşılığı (fiyat verisi okunmadı):

| Tarih | Taslakta | Durum |
|---|---|---|
| 2026-04-23 (Perş) | Ulusal Egemenlik ve Çocuk Bayramı, full_closure | resmi (bist_pay_2026_pdf) |
| 2026-05-01 (Cuma) | Emek ve Dayanışma Günü, full_closure | resmi |
| 2026-05-19 (Salı) | 19 Mayıs, full_closure | resmi |
| 2026-05-27, 28, 29 (Çar-Cum) | Kurban Bayramı, full_closure | resmi |
| 2026-07-15 (Çar) | Demokrasi ve Milli Birlik Günü, full_closure | resmi |

Hepsi resmi kapanış; Yahoo'nun bu günlerde satır düşürmesi/yer tutucu koyması resmi takvimle tutarlı. Not: 2026-05-26 (arife) yarım gün, tam işlem günü sayılmaz (13:00'e kadar seans); 2026-05-30 Cumartesi de tabloda kapalı.

## Kaynaklar (tam liste JSON `sources`)

- BIST Pay Piyasası tatil tabloları: `https://www.borsaistanbul.com/files/pay-piyasasi-{2023,2024,2025,2026}-yili-tatil-tablosu.pdf`
- BIST yıllık duyurular: `.../files/2023_tatil-duyurusu.pdf`, `.../files/Duyuru-31449.pdf` (2025), `.../files/41625-sayili-duyurumuz-hk.-turkce-04122025.pdf` (2026)
- BIST sayfa: `https://www.borsaistanbul.com/en/official-holidays` (13:00 saatleri; çapraz kontrol)
- Diyanet 2027: `https://vakithesaplama.diyanet.gov.tr/icerik.php?icerik=159` (resmi tatiller), `icerik=154` (dini günler)
- Üçüncü taraf (yalnız deprem kapanışı, doğrulanmadı): Gedik, QNB Invest, Timeturk
