# Sürekli veri kaynağı araştırması (öneri belgesi)

Hazırlık: 2026-09-28. Durum: **öneri** — karar Efe'nindir. Bu belge yatırım
tavsiyesi değildir; finansal değer içermez. Kod ve config değiştirilmemiştir.

## Erişim sınırı (önce okuyun)

Bu araştırma bir bulut oturumunda yapıldı ve **web erişimi kısıtlıydı**:

- Ortamın ağ politikası (egress proxy) şu alan adlarını **engelledi**:
  `borsaistanbul.com`, `datastore.borsaistanbul.com`, `kap.org.tr`, `mkk.com.tr`,
  `matriksdata.com`, `iqyardim.matriksdata.com`, `matriks.ai`, `foreks.com`,
  `fintables.com`, `tr.investing.com`, `aktifbank.com.tr`, `eodhd.com`,
  `twelvedata.com`, `guce.yahoo.com`. Bu sayfaların hiçbiri açılıp okunamadı.
- Çalışan yollar: (1) web arama motoru (yalnız başlık, URL ve arama aracının
  ürettiği kısa özet), (2) `github.com` sayfaları.
- Bu yüzden her olgusal iddia bir **kanıt etiketi** taşır:
  - **[GH]** — GitHub sayfası okundu (üçüncü taraf deposu; resmî kaynak değil).
  - **[AÖ]** — Yalnız arama sonucu özeti; birincil sayfa açılamadı.
    **Doğrulanmadı** sayılır. Özet, arama aracının ürettiği bir metindir ve hata
    içerebilir; aktarılan rakamlar birincil belgeden teyit edilmeden kullanılmamalı.
  - **[RP]** — Bu repodaki kod/belge (commit `6afd861`, `main`).
  - **[YOK]** — Bilgi bulunamadı / doğrulanmadı.
- Tüm web erişim tarihleri **2026-09-28**'dir. Hiçbir hesap açılmadı, giriş
  yapılmadı, sağlayıcıyla iletişim kurulmadı, Mini App'e bağlanılmadı.

Engeli kaldırmak için ortamın "Network access" ayarında yukarıdaki alan adları
izin listesine eklenebilir; sonra [AÖ] satırları birincil sayfadan yeniden
doğrulanmalıdır (açık soru S1).

## Özet

1. AKD (gün sonu "AKD", gün içi "AKDE") ve MKK takas verisi kullanıcı başı lisanslı ve ücretli; takas 2025-01-01'den beri [AÖ].
2. Otomatik (non-display) kullanım BIST ile ya da yetkili dağıtıcı üzerinden ayrı sözleşme ister; ekran lisansı yetmez [AÖ].
3. API'si belgelenmiş en somut aday Matriks veri servisleri (REST/MQTT/XML, bireysel satış, yeniden dağıtım yok) [AÖ].
4. Matriks API'sinde AKD+takas alanları, geçmiş derinliği, hız sınırı ve fiyat doğrulanmadı.
5. Bilinen tek halka açık aracı kurum API'si (Algolab) 2025-12-31'de kapatıldı [GH].
6. MKK e-Yatırımcı/VAP ve KAP kurum bazında günlük AKD/takas API'si sunmuyor (bulunamadı).
7. Mini App sürekli, insansız toplamaya uygun değil: insan `initData`, tek oturum, 429, belgesiz protokol, lisans bilinmiyor [RP].
8. Yahoo ve TradingView koşulları otomatik veri çekmeyi yasaklıyor [AÖ].
9. Öneri, birincil: AKD + MKK takas + OHLCV'yi otomatik kullanım için yazılı kapsayan lisanslı dağıtıcı servisi (ilk aday Matriks).
10. Öneri, yedek: ikinci lisanslı dağıtıcı (Foreks/ForInvest veya İdeal Data); OHLCV için BIST DataStore gün sonu dosyaları.
11. Maliyet bilinmiyor; ekran lisansı referansı 2026: AKD ₺173 + MKK ₺98 (tek aracı kurum tablosu) [AÖ].
12. Geçiş: önce yazılı lisans kapsamı ve alan tanımları, sonra geliştirme hisselerinde gün gün mutabakat.
13. Web erişimi kısıtlıydı: [AÖ] etiketli her iddia birincil sayfadan yeniden doğrulanmalı.

## Veri aileleri ve hedef

Hedef bot her işlem günü şunlara erişmeli: **AKD** (hisse × gün × aracı kurum:
alış/satış/net lot, tutar/maliyet), **takas** (hisse × gün × saklamacı üye: bakiye
lot ve pay), **OHLCV** (günlük). Derinlik ve gün içi ikincil.

Mevcut repo karşılıkları [RP]:

| Aile | Mevcut kaynak | Repo yapısı |
|---|---|---|
| AKD dönem neti | Mini App `get_akd` | `PeriodCapture.period_akd_net` (`net.size`) |
| AKD günlük | Mini App `custody_analysis` → `akd[date]` | `daily_akd_net` (`net_size`), `daily_akd_market` (`total_size`, `total_cost`) |
| Takas günlük bakiye | Mini App `custody_analysis` → `custody[date]` | `daily_custody_lots` (`lot_count`, kesirli olabilir) |
| Takas dönem farkı | Mini App `get_custodian_diff` | `custodian_diff` (`first_value`, `last_value`, `difference`) |
| Yatırımcı metadata | Mini App `custody_analysis.metadata` | `PeriodMetadataDay` (hesap yöntemi doğrulanmadı) |
| Kurum dizini | Mini App `get_brokerages` | `BrokerageDirectoryInfo` (`code/title/short_title`) |
| OHLCV | Yahoo chart v8 | `fintr_price_history_capture_v1` |

## Kaynak tablosu

Kısaltmalar: GS = gün sonu, Gİ = gün içi, ND = non-display (ekran dışı/otomatik)
kullanım. "?" = doğrulanmadı.

| # | Kaynak | AKD | Takas | OHLCV | Derinlik | Gecikme | Geçmiş | Erişim | Kimlik doğrulama | Hız sınırı | Maliyet (tarih) | Lisans / kullanım koşulları | Güvenilirlik | Mevcut veriyle karşılaştırılabilirlik |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BIST resmî veri yayını (veri dağıtıcılar üzerinden) | Evet: AKD (GS), AKDE (Gİ) [AÖ] | MKK saklama verisi 2025-01-01'den beri BIST lisanslı yayın [AÖ] | Evet [AÖ] | Evet (Level 1+/2/…) [AÖ] | Gerçek zamanlı, 15 dk gecikmeli, GS [AÖ] | ? | Dağıtıcıya bağlı | Dağıtıcı sözleşmesi | ? | Ekran lisansı örneği: 2026 AKD ₺173, MKK ₺98 (tek aracı kurum tablosu; dönem/aylık olduğu 2026 için doğrulanmadı) [AÖ] | ND ve türetilmiş veri için Veri Dağıtım Sözleşmesi gerekli [AÖ] | Birincil (borsa) kaynak | Yüksek beklenir; alan tanımları doğrulanmalı |
| 2 | BIST DataStore (geçmiş dosya satışı) | Üye bazlı yıllık/aylık/haftalık işlem dağılımı dosyaları var [AÖ]; hisse×gün×kurum AKD dosyası **doğrulanmadı** | Yok (bulunamadı) | GS bülten dosyaları [AÖ] | ? | GS / dosya | 2015 sonrası BISTECH formatları [AÖ]; tam derinlik ? | Web portalı, dosya | Portal hesabı ? | ? | ? | Kullanım koşulları okunamadı | Resmî | OHLCV için yüksek; AKD için belirsiz |
| 3 | BIST VERDA / Veri Analitikleri (VAS) | Alıcı-satıcı analitikleri [AÖ]; kurum kırılımı ? | Yok | ? | ? | Gİ mesajlar [AÖ] | ? | HTTP-REST, mesaj [AÖ] | BIST veri yayın kullanıcı adı/şifre [AÖ] | ? | ? | Kurumsal (üye, dağıtıcı) odaklı [AÖ] | Resmî | Bireysel erişim gerçekçi değil |
| 4 | Matriks veri servisleri (Analist REST, MQTT, XML Feeder, DB Matriks) | Terminalde formülle erişilebilir [AÖ]; API'de ? | Terminalde ("takas") [AÖ]; API'de ? | Tarihsel REST [AÖ] | Terminalde [AÖ] | Gerçek zamanlı / 15 dk gecikmeli stream (MQTT) [AÖ] | ? | REST, MQTT (WebSocket/TCP), XML | ? | ? | Veri servisi ücreti ?; BIST lisans ücretleri ayrıca uygulanır [AÖ] | "Bireysel veya kurumsal kullanıma uygun"; **yeniden dağıtım yok** [AÖ] | Lisanslı dağıtıcı olduğu vendor listesinden **doğrulanmadı** | Alan tanımı ve kurum kodları ? |
| 5 | Foreks / ForInvest | ? | 2025'te "MKK Saklama Verileri lisanslı hâle geliyor" duyurusu [AÖ] | Evet [AÖ] | ? | Gerçek zamanlı [AÖ] | ? | Terminal/uygulama; **herkese açık fiyat API'si yok** [AÖ] | ? | ? | ? | "Yalnız müşterinin bireysel kullanımı" [AÖ] | SPK lisanslı veri dağıtım kuruluşu olduğunu söylüyor [AÖ] | ? |
| 6 | İdeal Data (iDeal) | Terminalde; AKD lisansı tabloda [AÖ] | Terminalde takas analizi [AÖ] | Evet | Evet | Gerçek zamanlı | "Geçmiş veri sağlama" hizmeti [AÖ] | Terminal; API ? | ? | ? | 2026 ekran lisansları: AKD ₺173, MKK ₺98 (iDeal için aracı kurum tablosu) [AÖ] | ? | ? | ? |
| 7 | Fintables | AKD ekranları [AÖ] | "MKK Verileri Lisansı" ile [AÖ] | ? | ? | ? | ? | Web; **kurumsal pakette API** [AÖ] | Hesap | ? | ? | Takas için BIST lisansı gerekiyor [AÖ] | ? | ? |
| 8 | Aracı kurum API'si — Deniz Yatırım Algolab | Kanıt yok | Kanıt yok | Canlı veri [AÖ] | İsteğe bağlı derinlik [AÖ] | Gİ | ? | REST + WebSocket [AÖ] | API anahtarı + kullanıcı/şifre [GH] | ? | — | **2025-12-31'de kapatıldı** [GH] | Kullanılamaz | — |
| 9 | Diğer aracı kurum API'leri | Bulunamadı | Bulunamadı | ? | ? | ? | ? | ? | ? | ? | ? | ? | ? | — [YOK] |
| 10 | MKK e-Yatırımcı | Hayır (kişinin kendi hesabı) [AÖ] | Kişisel bakiye, piyasa değil | Hayır | Hayır | — | — | Web | Kişisel giriş | — | Ücretsiz [AÖ] | — | Resmî | Uygun değil |
| 11 | MKK VAP (vap.org.tr) | Hayır | Toplulaştırılmış istatistik (yatırımcı sayısı, yerli/yabancı saklama oranı; aylık/yıllık) [AÖ]; kurum kırılımı yok | Hayır | Hayır | Aylık/yıllık [AÖ] | ? | Web | ? | ? | ? | Okunamadı | Resmî | Takas bakiyesiyle aynı tanım değil |
| 12 | KAP | Hayır | Hayır | Hayır | Hayır | Bildirim anlık | — | Web, mobil, **KAP Veri Yayın Hizmeti**, SWIFT [AÖ] | ? | ? | ? | Okunamadı | Resmî | Yalnız kurumsal işlem/olay kaydı için |
| 13 | Yahoo chart v8 (mevcut) | Hayır | Hayır | Evet [RP] | Hayır | GS [RP] | 2023-01-02'den beri kayıtlı [RP] | Gayriresmî JSON | Yok | 429 görülebilir [RP] | Ücretsiz | Otomatik erişim, izin olmadan yasak [AÖ] | İkincil; ölçek/hacim rejimi sorunu gözlendi [RP] | Mevcut referans |
| 14 | TradingView (borsapy vb. kütüphaneler) | Hayır | Hayır | ~15 dk gecikmeli [GH] | Hayır | Gecikmeli | ? | WebSocket (gayriresmî) [GH] | ? | ? | — | Otomatik veri toplama ve ND kullanım yasak [AÖ]; borsapy: "yalnız kişisel/eğitim" [GH] | Gayriresmî | Uygun değil |
| 15 | İş Yatırım web JSON uçları | Hayır | Hayır | Evet (tarihsel fiyat sayfası) [GH] | Hayır | GS | ? | Gayriresmî JSON | Yok | ? | — | Kullanım koşulları okunamadı | `HisseTekil` repoda hata verdi [RP] | Uygun değil |
| 16 | Uluslararası dağıtıcılar (EODHD, Twelve Data "XIST", dxFeed, ICE, LSEG, Bloomberg) | Kanıt yok | Kanıt yok | Muhtemelen (sayfalar BIST'i listeliyor) [AÖ] | ? | ? | ? | API | API anahtarı | ? | ? | ? | ? | Yalnız OHLCV için yedek adayı |
| 17 | Mini App "Veri Terminali" (mevcut) | Evet [RP] | Evet [RP] | Fiyat protokolü keşfedilmedi [RP] | Yanıt parser'ı var, istek şeması yok [RP] | GS; aynı gün eksik olabilir [RP] | ~1 yıl kayıt alındı [RP] | Belgesiz WebSocket [RP] | Telegram `initData`, insan girer [RP] | 429; tek oturum [RP] | Kendini "ücretsiz" tanıtıyor [AÖ] | Lisans/izin **doğrulanmadı**; izin kaydı yalnız kullanıcı beyanı [RP] | Belgesiz | Referans (tanım) kaynağı |

## Ayrıntılar

### 1. BIST resmî veri yayını ve lisanslar

- BIST piyasa verisini gerçek zamanlı, gecikmeli ve gün sonu olarak lisanslı veri
  dağıtıcıları üzerinden yayar; kullanıcı, BIST'in veri dağıtıcı kuruluşlar
  listesindeki kuruluşlara yönlendirilir [AÖ: BIST Veri Yayını, Veri Dağıtıcı
  Kuruluşlar]. **Liste okunamadı**; hangi firmaların (Matriks, Foreks, İdeal vb.)
  listede olduğu doğrulanmadı (açık soru S2).
- AKD: "Pay Piyasası sembollerinde gerçekleşen işlemlere ait alıcı ve satıcı taraf
  bilgisini gün sonunda sunar"; AKDE aynı bilgiyi seans içinde eşanlı sunar; yalnız
  gün sonu için AKD lisansı yeterlidir [AÖ: Matriks blog, Matriks IQ yardım].
- Takas: Fintables ve Foreks duyurularına göre saklama bakiyesi verileri 2025-01-01'den
  itibaren BIST tarafından ücretli ve veri yayın lisansı gerektirecek şekilde
  düzenlenmiştir; yeni veri setine "yatırımcı sayısı, kurumsal/bireysel yatırımcı
  sayıları ve yüzdeleri" eklenmesi duyurulmuştur [AÖ: Fintables X gönderisi,
  Foreks duyurusu].
- MKK genel mektubu: hisse senedi üye ve bakiye bilgileri **saatlik** olarak Takasbank
  aracılığıyla veri yayın kuruluşlarına iletilir; birden fazla üye koduna sahip
  üyeler bu bilgilerin **tek üye kodunda konsolide** yayımlanmasını talep edebilir
  [AÖ: MKK Genel Mektup]. Sonuç: kaynaklar arasında kurum kodu kümesi farklı
  olabilir (bkz. doğrulama planı D3).
- Non-display: gözetim, risk yönetimi, otomatik/algoritmik işlem, yatırım analiz
  hizmetleri gibi ekransız kullanımlar için BIST ile doğrudan ya da yetkili dağıtıcı
  üzerinden Veri Dağıtım Sözleşmesi gerekir [AÖ: BIST SSS]. Tarama/bildirim botu
  büyük olasılıkla bu kapsama girer; kesin sınıflandırma sağlayıcı/BIST'ten yazılı
  alınmalı (S3).
- Türetilmiş veri: "orijinal değerin yerine geçmeyecek ve geri mühendislikle orijinale
  dönülemeyecek" şekilde işlenmiş veri; endeks değilse Veri Dağıtım Sözleşmesi
  gerekir [AÖ: BIST SSS]. Botun bildirimleri (ör. oranlar, koşul gözlemleri) bu
  tanıma girebilir; yalnız kişisel kullanımda istisna olup olmadığı **doğrulanmadı**.
- Gecikmeli veri 15 dakikadır; kullanıcı başı ücrete tabi olmayan veriler (sınırlı
  Level 1, gecikmeli, referans) için alt dağıtıcı hakkı alınabilir [AÖ: BIST SSS].
- Ücret: BIST ücret tarifesi Veri Dağıtım Sözleşmesi Ek-3'tedir [AÖ]; okunamadı.
  Aracı kurum tablolarındaki son kullanıcı ekran lisansı referansları:
  - "01-01-2026 Veri Lisans Ücret Tablosu" (ATA Online, iDeal): AKD ₺173, MKK ₺98,
    VAKD ₺188 [AÖ]. Birimin aylık olduğu 2026 tablosu için doğrulanmadı.
  - "01-01-2025" tablosu (Aktifbank, iDeal): AKD ₺139, AKDE ₺357 aylık [AÖ].
  - Bunlar **ekran** lisanslarıdır; API/ND ücreti değildir.

### 2. BIST DataStore ve VERDA

- Geçmiş ve referans veri dosyaları 2015-08-01'den beri yalnız DataStore'da
  satılır [AÖ]. Pay piyasası "veri bildirim ve kabul formatları" belgesi mevcut
  [AÖ]; içerik okunamadı.
- Arama özetinde üye bazlı yıllık işlem dağılımı (`PP_UYEISLEM.Y.[YYYY].xlsx`),
  haftalık üye sıralamaları, aylık kümülatif dağılım gibi dosyalar geçiyor [AÖ].
  Bunlar hisse × gün × kurum AKD'si değildir; günlük AKD dosyası **doğrulanmadı**.
- VERDA HTTP-REST API, aracı kurumlar, veri yayın kuruluşları ve endeks lisans
  şirketleri için dosya erişimi sağlar; VAS (Veri Analitikleri) alıcı-satıcı
  analitikleri içerir ve BISTECH veri yayın kimliği ister [AÖ]. Bireysel araştırmacı
  için uygunluğu düşük görünür; doğrulanmadı.

### 3. Lisanslı veri sağlayıcıları

- **Matriks:** XML web servis, MQTT/socket ve REST servisleri; "bireysel veya
  kurumsal kullanıma uygun veri servisleri satılmaktadır"; gerçek zamanlı ve 15 dk
  gecikmeli stream MQTT ile, tarihsel ve hesaplanmış veri REST ile; "sağlanan
  verinin içeriğine göre BIST lisans ücretleri uygulanır"; "veriler yeniden dağıtma
  opsiyonu ile sağlanmamaktadır" [AÖ: Matriks SSS ve ürün sayfaları]. AKD ve takas
  terminalde (MatriksIQ formül penceresi) mevcut [AÖ]; **API'de bu ailelerin, geçmiş
  derinliğinin, hız sınırlarının ve fiyatın olduğu doğrulanmadı.**
- **Foreks/ForInvest:** SPK lisanslı veri dağıtım kuruluşu olduğunu belirtir;
  "herkese açık fiyat API'si yoktur"; lisanslı ürün yalnız müşterinin bireysel
  kullanımı içindir [AÖ: foreks.com/llms.txt]. Programatik erişim ürünü bulunamadı.
- **İdeal Data:** terminal, beyaz etiket platformlar, "geçmiş veri sağlama" ve özel
  çözümler [AÖ]. API varlığı doğrulanmadı.
- **Fintables:** kurumsal üyelik paketlerinde API ile veri çekme; takas yalnız
  "MKK Verileri Lisansı" alan üyelere [AÖ]. Fintables'ın lisanslı dağıtıcı mı alt
  dağıtıcı mı olduğu ve API'de AKD/takas bulunduğu doğrulanmadı.
- Uluslararası (EODHD, Twelve Data, dxFeed, ICE, LSEG, Bloomberg): arama sonuçları
  BIST kapsamı sayfalarını listeliyor [AÖ]; hiçbiri için AKD/takas kanıtı yok.
  En fazla OHLCV yedeği olarak düşünülebilir.

### 4. Aracı kurum API'leri

- Deniz Yatırım Algolab: REST + WebSocket; canlı fiyat, isteğe bağlı derinlik, emir
  ve portföy işlemleri [AÖ]. Üçüncü taraf Python sarmalayıcısının README'si:
  "Algolab Platformu ve API hizmeti 31.12.2025 itibarıyla kullanıma kapatılmıştır"
  [GH]. AKD/takas uç noktasına dair kanıt bulunamadı.
- Başka bir Türk aracı kurumunun halka açık, AKD/takas içeren veri API'si
  bulunamadı [YOK]. Aracı kurum API'leri genelde emir iletimi odaklıdır; veri
  kullanımı yine BIST lisanslarına tabidir (varsayım; doğrulanmadı).

### 5. MKK / e-Yatırımcı / KAP

- e-Yatırımcı yatırımcının **kendi** hesap bilgisidir [AÖ]; piyasa geneli kurum
  bazında saklama vermez.
- VAP (vap.org.tr) yatırımcı sayıları, yerli/yabancı saklama oranları gibi
  toplulaştırılmış, aylık/yıllık istatistikler sunar [AÖ]. Kurum bazında günlük
  bakiye yok (bulunamadı). Repodaki `retail_pct/investor_count` metadata'sıyla
  **aynı tanımda olduğu varsayılamaz**; yalnız kaba tutarlılık referansı olabilir.
- KAP bildirimleri web, mobil, "KAP Veri Yayın Hizmeti" ve SWIFT ile iletilir [AÖ].
  Koşulları ve bireysel erişimi doğrulanmadı. Repo açısından kullanım: kurumsal
  işlem kaydı (`configs/corporate_actions/`) — STATUS'a göre KAP tarayıcıda da
  engelli, belgeler Efe'den geliyor [RP].

### 6. Kamuya açık web kaynakları

- Yahoo: Kullanım koşulları robot/scraper vb. otomatik yollarla, izin olmadan veri
  çıkarmayı yasaklar [AÖ; koşul sayfası okunamadı]. Repo izni
  `user_asserted_2026-09-26_not_independently_verified` olarak kaydediyor [RP].
- TradingView: otomatik veri toplama ve ND kullanım yasak; içerik yalnız görüntüleme
  içindir [AÖ]. borsapy OHLCV'yi TradingView WebSocket'ten (~15 dk gecikmeli) alır ve
  "yalnız kişisel ve eğitim amaçlı" uyarısı taşır [GH].
- İş Yatırım: tarihsel fiyat sayfası JSON uçları üçüncü taraf örneklerinde kullanılıyor
  [GH]; kullanım koşulları okunamadı; repoda `HisseTekil` hata verdi [RP].
- Bu kaynakların hiçbiri AKD veya takas sağlamıyor (bulunamadı).

### 7. Mini App akışının sürekli kullanım değerlendirmesi

Yalnız kod ve belgeler incelendi; canlı bağlantı kurulmadı.

**Teknik [RP]:**

| Konu | Durum | Sürekli toplamaya etkisi |
|---|---|---|
| Kimlik | `InteractiveSecretProvider` `initData`'yı `getpass` ile alır; saklamaz; yenileme, Telegram login ve tarayıcı otomasyonu yok (`secrets.py`, collector.md "Güncel sınırlamalar") | İnsansız çalışma, `initData`'nın bir yerde saklanmasını ve yenilenmesini gerektirir; bu repo'nun secret politikasına aykırı. `initData` ömrünün sunucuda nasıl denetlendiği doğrulanmadı |
| Oturum | Tek aktif oturum; collector Mini App'i kapatabilir; `--allow-session-takeover` zorunlu | Zamanlanmış iş, Efe'nin Mini App kullanımıyla çakışır |
| Hız | STATUS: 429 hız sınırı; çok hisseli kayıtta `--symbol-pause-seconds 30`; oturum düzeyinde 2 ardışık başarısızlıkta durma | Günlük artımlı yük (30 hisse × ~3 istek, 1 gün) bir yıllık geri doldurmadan çok küçüktür; teknik olarak düşük hızda sığabilir, ama sınırın değeri bilinmiyor |
| Protokol | Belgesiz; JS bundle hash adlı; önceki sessiz denemelerin nedeni bilinmiyor; bilinmeyen event tipleri görüldü | Her an kırılabilir; üretim izleme ve hızlı parser değişikliği gerekir |
| Veri zamanlaması | Aynı gün verisi eksik olabilir; gelecek tarih koruması; takas bir sonraki güne kadar kesinleşmeyebilir (runbook) | Zamanlama gün sonrası (ör. T+1 sabahı) olmalı |
| Kalıcılık | Collector diske yazmaz; kalıcı yazım yalnız dönem kaydı/pilot yollarında | Günlük artımlı kayıt şeması yok |
| Kanıt | `source: live/pg`, `cached` etiketleri kaynak/lisans garantisi değil; request-id korelasyonu yok | Provenance zayıf |

**Koşullar:** Mini App'in kullanım koşulları repoda veya erişilebilir kaynakta yok.
Kendini ücretsiz AKD/takas/derinlik sağlayan bir Telegram uygulaması olarak tanıtıyor
[AÖ: Telegram bot sayfası, X gönderisi]. BIST'e göre bu veriler kullanıcı başı
lisanslıdır [AÖ]. Uygulamanın BIST lisanslı dağıtıcısı ya da alt dağıtıcısı olup
olmadığı **doğrulanamadı** (dağıtıcı listesi okunamadı). Bu belge bir ihlal iddia
etmez; yalnız lisans zincirinin bilinmediğini kaydeder. Otomatik, zamanlanmış
kullanımın uygulamanın izin verdiği bir kullanım olduğu da doğrulanmadı.

**Sonuç:** Birincil sürekli kaynak olarak uygun değil. Geçiş döneminde yalnız insan
tarafından başlatılan, mutabakat referansı sağlayan tek seferlik kayıtlar için
(mevcut kullanım gibi) kalabilir.

## Öneri

Bu bölüm önerisidir; hiçbir sağlayıcıyla iletişim kurulmamıştır.

**Birincil:** BIST lisanslı bir veri dağıtıcısının programatik veri servisi;
sözleşme **yazılı olarak** şunları kapsamalı: AKD (gün sonu yeterli) + MKK saklama
(takas) + günlük OHLCV; tek kullanıcılı kişisel araştırma botu için otomatik/ND
kullanım; yerel saklama; türetilmiş gözlemlerin yalnız Efe'ye bildirimi. İlk aday:
**Matriks veri servisleri** (REST/MQTT belgelenmiş, bireysel satış var) [AÖ].

**Yedek:** aynı sözleşme soruları ile ikinci bir lisanslı dağıtıcı (Foreks/ForInvest
veya İdeal Data; ya da kurumsal API'si olan Fintables). OHLCV için ek yedek: BIST
DataStore gün sonu dosyaları (resmî, dosya bazlı).

**Gerekçe:** AKD ve takas yalnız lisanslı kanaldan yasal ve istikrarlı biçimde
alınabiliyor; Mini App ve web kaynakları otomasyon, lisans ve protokol açısından
sürekli çalışmaya uygun değil; tek sağlayıcıdan üç ailenin gelmesi tarih/kod
eşleşmesini basitleştirir.

**Tahmini maliyet:** bilinmiyor. Alt sınır referansı yalnız ekran lisanslarıdır:
2026 için AKD ₺173 + MKK ₺98 (tek aracı kurum tablosu, [AÖ]). Veri servisi ücreti ve
ND lisans ücreti ayrıca ve muhtemelen daha yüksektir; **doğrulanmadı**.

**Riskler:**

1. ND/otomatik kullanım lisansı bireysele satılmayabilir veya pahalı olabilir.
2. API'de AKD ya da takas bulunmayabilir (yalnız terminalde olabilir).
3. Geçmiş derinliği yetersiz olabilir; holdout testi ~Nisan 2027'de yine mevcut
   kaynağa bağlı kalabilir (tanım farkı riski, bkz. D-planı).
4. Alan tanımları (net lot, maliyet, bakiye birimi, konsolide kurum kodu) mevcut
   kayıtlardan farklı olabilir → çalışmaların ön kayıtlı koşulları kaynak değişince
   aynı anlamı taşımayabilir.
5. Tek sağlayıcı bağımlılığı; yedek için ikinci sözleşme maliyeti.
6. Yeniden dağıtım yasakları: bildirimler başkalarıyla paylaşılırsa ek lisans gerekir.

## Geçiş taslağı

Yalnız tasarım; kod yazılmadı. Uygulamadan önce AGENTS.md gereği kısa bir ADR veya
`docs/architecture.md` güncellemesi yazılmalı.

### Kaynak adaptörü arayüzü

Amaç: mevcut `period_capture`, `price_history` ve özellik katmanını değiştirmeden
kaynağı değiştirebilmek. Yeni kaynak mevcut canlı modellere (`Live*Response`)
**zorla dönüştürülmez**; kaynağa özgü typed modeller ve ortak, kaynak-nötr bir günlük
kayıt kullanılır.

```text
SourceAdapter (Protocol)
  source_id: str                       # "matriks_rest_v1" gibi; sürümlü
  capabilities() -> SourceCapabilities
      families: {"akd_daily", "custody_daily", "ohlcv_daily", "brokerage_directory"}
      latency_class: "eod" | "delayed_15m" | "realtime"
      history_start: date | None       # sağlayıcı beyanı, doğrulama durumuyla
      license_ref: LicenseRef          # sözleşme kimliği, tarih, kapsam bayrakları
  plan(req: DailyCollectionRequest) -> tuple[PlannedCall, ...]
      # ağsız; dry-run çıktısı; hedef yol ve no-overwrite kontrolü
  fetch(call: PlannedCall, secrets: SecretProvider) -> RawCapture
      # ham bayt + HTTP durum + zaman; secret asla RawCapture'a girmez
  normalize(raw: RawCapture) -> SourceDailyRecord
      # katı parse; Decimal; null≠0; birim/ölçek alan bazında etiketli

DailyCollectionRequest: symbol, trade_date, families, split_role (dev/holdout/excluded)
SourceDailyRecord:
  akd_rows:      (broker_code, buy_lots?, sell_lots?, net_lots, buy_value?, sell_value?,
                  total_lots?, total_value?)   # kaynakta olmayan alan None, türetilmez
  custody_rows:  (member_code, balance_lots, share, share_scale="fraction"|"percent")
  ohlcv:         (open, high, low, close, volume, adjusted_close?, adjustment_policy)
  broker_directory: (code, title, short_title?, consolidated_from?: tuple[str, ...])
  source_dates:  trade_date, custody_as_of (T, T+1, T+2 semantiği açık), published_at
```

Uyum katmanı (ayrı, test edilebilir): `SourceDailyRecord` dizisinden bir dönem
paketi üretir. Seçenek A: yeni paket şeması `fintr_period_analysis_handoff_v3`
(`source_id` ve kaynak sürümü zorunlu). Seçenek B: kaynak-nötr günlük depo +
`PeriodCapture` benzeri yükleyici. Öneri A; mevcut v1/v2 yükleyici davranışı değişmez,
v3 yalnız ek alanlar getirir. Karar ADR'de verilmeli.

İlkeler: holdout ve dışlama kuralları (`configs/splits/current.json`,
`configs/exclusions/symbols.json`) adaptör katmanında da uygulanır — toplama izinli,
analiz reddedilir; DSTKF toplanmaz. Hız sınırı ve retry politikası config'te.
Secret yalnız çalışma zamanı sağlayıcıdan (ör. OS keyring / ortam değişkeni; seçim
ayrı karar).

### Doğrulama planı

Kural: mutabakat yalnız **geliştirme hisselerinde** ve **2026-09-25 veya öncesi**
günlerde yapılır; holdout hisselerinin değerleri ve 2026-10-01 sonrası test penceresi
mutabakat girdisi olmaz (holdout için yalnız yapısal kontroller: satır sayısı, kod
kümesi, şema). Kabul eşikleri **veri görülmeden önce** Efe tarafından dondurulur;
aşağıdaki eşikler yalnız öneridir.

- **D1 — AKD mutabakatı.** Aynı (hisse, gün) için yeni kaynağın `net_lots`'u ile
  kayıtlı `daily_akd_net` (`net_size`) kurum bazında karşılaştırılır. Çıktı: tam
  eşleşme oranı, yalnız bir tarafta olan kodlar, fark dağılımı. Öneri: ortak kodlarda
  ≥%99 tam eşleşme (lot tamsayıysa). Ayrıca `total_size/total_cost` karşılıkları
  belirlenirse aynı test. `Σ net ≈ 0` beklenen bir özdeşliktir ama kaynakta doğrulanmadan
  varsayılmaz; iki kaynakta ayrı ayrı ölçülüp raporlanır.
- **D2 — Takas farkı tutarlılığı.** (a) Bakiye: `balance_lots` ↔ `daily_custody_lots`
  (kesirli değerler korunur). (b) Fark: `lot[t] − lot[p]` ↔ `custodian_diff.difference`.
  (c) Akış: mevcut `custody_flow_v3` artığı (`residual = Δlot − akd_net`) iki kaynakla
  ayrı hesaplanır; artık dağılımlarının benzerliği ve takas tarihinin işlem gününe göre
  kayması (T+2 varsayımı **doğrulanmadı**) raporlanır. (d) Pay ölçeği (oran/yüzde)
  açıkça normalize edilir; ham karşılaştırılmaz.
- **D3 — Kurum kodu eşleşmesi.** Yeni kaynağın kod dizini ile kayıtlı
  `brokerage_directory` tam kod eşleşmesi (fuzzy yok). Konsolide üye kodları (MKK genel
  mektubu) ayrı listelenir; eşleme tablosu elle onaylanır, `unresolved` açıkça kalır.
- **D4 — OHLCV.** Yeni kaynak ↔ Yahoo: kapanış eşitliği, hacim oranı (bilinen "sabit
  1/k" rejimi), düzeltilmiş/düzeltilmemiş politika, tatil yer tutucu satırları.
- **D5 — Süreklilik denemesi.** Sözleşme sonrası 10 işlem günü gölge çalışma: her gün
  zamanında veri, eksik gün, gecikme, hata sınıfları; Mini App kayıtlarıyla D1–D3 günlük.
- **D6 — Çalışma etkisi.** Ön kayıtlı H1–H4 koşullarının girdileri kaynak değişince
  aynı mı? Tanım farkı varsa holdout testi hangi kaynakla yapılacak kararı
  `docs/prereg/DECISIONS.md`'de, test öncesi (S7).

Her doğrulama çıktısı tekrar çalıştırılabilir komut + metrik artefact'ı (commit, config,
veri manifesti, sürüm) ile saklanır; finansal değer yazdırmaz.

### Provenance alanları

Her ham ve normalize kayıtta:

| Alan | Açıklama |
|---|---|
| `schema` | ör. `fintr_source_daily_capture_v1` |
| `source_id`, `source_version` | adaptör ve sağlayıcı API sürümü |
| `vendor_legal_name`, `product_name`, `endpoint` | endpoint secret içermez |
| `license_ref` | sözleşme no/tarihi, kapsam bayrakları: `personal`, `non_display`, `storage`, `derived_output`, `redistribution=false` |
| `permission_provenance.status` | ör. `contract_signed_YYYY-MM-DD` veya `user_asserted_..._not_independently_verified` |
| `bist_license_codes` | ör. AKD, MKK (sağlayıcının belirttiği) |
| `request` | sembol, işlem günü, aile, parametreler (secret'sız) |
| `accessed_at_utc`, `http_status`, `rate_limit_observed` | erişim ve hız sınırı gözlemi |
| `source_timestamps` | `trade_date`, `custody_as_of`, `published_at`, sağlayıcı `as_of` |
| `latency_class` | eod/delayed/realtime |
| `raw_sha256`, `normalized_sha256` | kanonik JSON ile |
| `units` | alan başına birim/ölçek (lot, TL, oran/yüzde) ve kaynağı (sağlayıcı belgesi/gözlem) |
| `transformation` | normalizasyon adımları, `adapter_git_commit`, config hash |
| `split_role` | dev/holdout/excluded; `analysis_allowed` |
| `retention` | saklama süresi ve lisans dayanağı |

## Açık sorular

- **S1.** [AÖ] işaretli her iddia birincil sayfadan doğrulanmalı (ağ izni gerekir).
- **S2.** BIST veri dağıtıcı listesinde hangi firmalar var? Matriks, Foreks/ForInvest,
  İdeal Data, Fintables'ın rolü (dağıtıcı/alt dağıtıcı)?
- **S3.** Tek kişilik, bildirim üreten araştırma botu BIST'e göre ND kullanım mı?
  Kişisel kullanım istisnası var mı? Türetilmiş gözlemlerin bildirimi serbest mi?
- **S4.** Adayların API'sinde AKD (alış/satış/net lot, tutar) ve MKK takas (kurum
  bakiyesi) var mı; kaç yıllık geçmiş; hız sınırları; fiyat?
- **S5.** Takas bakiyesi tarihi hangi işlem gününü yansıtıyor (T+2 takas, saatlik
  güncelleme)? Mini App `custody[date]` ile aynı mı?
- **S6.** Kurum kodları: konsolide üye kodları ve MKK saklamacı kodu ↔ BIST üye kodu
  eşlemesi.
- **S7.** Holdout testi (2027-03-31 sonrası) mevcut Mini App kaynağıyla mı, yeni
  kaynakla mı? Kaynak değişimi ön kayıt değişikliği sayılır mı?
- **S8.** Mini App'in kullanım koşulları ve lisans zinciri nedir? (İletişim bu çalışmada
  yapılmadı.)
- **S9.** Yahoo yerine OHLCV'yi de lisanslı kaynaktan almak, mevcut fiyat geçmişiyle
  süreklilik için geriye dönük dolum gerektirir mi?

## Kaynakça

Tüm erişim tarihleri 2026-09-28. Etiket: [GH] okundu; [AÖ] yalnız arama özeti,
sayfa proxy tarafından engellendi.

Borsa İstanbul
- [AÖ] Veri Yayını — <https://www.borsaistanbul.com/en/data/data-dissemination>
- [AÖ] Veri Dağıtıcı Kuruluşlar — <https://www.borsaistanbul.com/en/data/data-dissemination/data-vendors-directory>, <https://borsaistanbul.com/tr/sayfa/60/veri-dagitici-kuruluslar>
- [AÖ] Veri Yayın Ürünleri — <https://www.borsaistanbul.com/veriler/veri-yayini/veri-yayin-urunleri>
- [AÖ] SSS: Veri Dağıtım ve Endeks Lisanslama — <https://www.borsaistanbul.com/sss/veri-dagitim-ve-endeks-lisanslama>
- [AÖ] SSS: Veri - Endeks Yayını ve Satışı — <https://www.borsaistanbul.com/sss/veri-endeks-yayini-ve-satisi>
- [AÖ] Veri Dağıtım Sözleşmesi — <https://www.borsaistanbul.com/en/data/data-dissemination/borsa-istanbul-data-distribution-agreement>
- [AÖ] Geçmişe Dönük Veri Satışı — <https://www.borsaistanbul.com/en/data/historical-data-sales>
- [AÖ] DataStore — <https://datastore.borsaistanbul.com/>
- [AÖ] DataStore Veri Bildirim ve Kabul Formatları — <https://datastore.borsaistanbul.com/assets/files/DataStore_Veri_Bildirim_ve_Kabul_Formatlar%C4%B1.pdf>
- [AÖ] BISTECH Pay Piyasası Veri Analitikleri — <https://www.borsaistanbul.com/files/veri-analitikleri-tip-yayin-formati.pdf>
- [AÖ] BISTECH VERDA HTTP-Rest API — <https://www.borsaistanbul.com/files/bistech-verda-http-rest-api-integration-manual_tr.pdf>

MKK / KAP
- [AÖ] Genel Mektup: Veri Yayın Kuruluşlarına Gönderilen Veriler — <https://www.mkk.com.tr/tr-tr/Genel-Mektuplar/Sayfalar/Veri-Yayin-Kuruluslarina-Gonderilen-Veriler-Hk.aspx>
- [AÖ] e-Yatırımcı — <https://eyatirimci.mkk.com.tr/>
- [AÖ] VAP — <https://www.vap.org.tr/en>, MKK duyurusu <https://www.mkk.com.tr/haberler/mkk-piyasa-ekrani-yeni-arayuzuyle-vap-veri-analiz-platformunda-vaporgtr-yayimda>
- [AÖ] KAP (MKK sayfası) — <https://www.mkk.com.tr/en/corporate-governance-services/kap-public-disclosure-platform>

Sağlayıcılar ve lisans ücretleri
- [AÖ] Matriks blog: BIST Veri Lisansları — <https://www.matriksdata.com/website/blog/borsa-istanbul-bist-veri-lisanslari>
- [AÖ] Matriks IQ yardım: BIST Veri Lisansları — <https://iqyardim.matriksdata.com/docs/non-knowledgebase/sikca-sorulan-sorular/borsa-istanbul-bist-veri-lisanslari/>
- [AÖ] Matriks: Veri ve İçerik Sağlayıcı Servisler SSS — <https://www.matriksdata.com/website/egitim/sikca-sorulan-sorular/veri-ve-icerik-saglayici-servisler-sss>
- [AÖ] Matriks: Veri ve İçerik Sağlayıcı Servisler — <https://www.matriksdata.com/website/urunlerimiz/kurumsal-hizmet-ve-servisler/veri-ve-icerik-saglayici-servisler>
- [AÖ] ForInvest/Foreks — <https://www.foreks.com/llms.txt>
- [AÖ] Foreks duyurusu (investing.com) — <https://tr.investing.com/news/economy-news/forinvest-abonelerine-duyurumkk-saklama-verileri-2025-ylnda-lisansl-hale-geliyor-3194801>
- [AÖ] İdeal Data — <https://idealdata.com.tr/urunler/ideal/>
- [AÖ] Fintables X gönderisi — <https://x.com/fintables/status/1871884891274514674>
- [AÖ] Fintables üyelik paketleri — <https://fintables.com/uyelik-paketleri>
- [AÖ] 01-01-2026 Veri Lisans Ücret Tablosu (ATA Online) — <https://cdn.ataonline.com.tr/aol/contentfiles/VeriEkranUcret.pdf>
- [AÖ] 01-01-2025 Veri Lisans Ücret Tablosu (Aktifbank) — <https://www.aktifbank.com.tr/uploads/20250509104629568.pdf>
- [AÖ] EODHD — <https://eodhd.com/list-of-stock-markets>; Twelve Data XIST — <https://twelvedata.com/exchanges/XIST>; dxFeed Türkiye — <https://dxfeed.com/coverage/turkey/>; ICE — <https://developer.ice.com/fixed-income-data-services/catalog/borsa-istanbul>

Aracı kurum API'leri
- [GH] atillayurtseven/AlgoLab (kapanış notu) — <https://github.com/atillayurtseven/AlgoLab>
- [AÖ] Algolab API Hizmeti — <https://www.algolab.com.tr/ApiService>

Web kaynakları ve koşullar
- [AÖ] Yahoo Terms of Service — <https://guce.yahoo.com/terms?locale=en-US>
- [AÖ] TradingView Policies — <https://www.tradingview.com/policies/>
- [GH] saidsurucu/borsapy — <https://github.com/saidsurucu/borsapy>
- [GH] urazakgul/veri-kaynaklari-python — <https://github.com/urazakgul/veri-kaynaklari-python>

Mini App
- [AÖ] Veri Terminali Telegram sayfası — <https://t.me/ucretsizderinlikbot>
- [AÖ] X gönderisi ("ücretsiz derinlik bot") — <https://x.com/AnKaRaLI0678/status/2044324542655438931>

Repo [RP]: `docs/STATUS.md`, `docs/collector.md`, `docs/protocol_inventory.md`,
`docs/live_check_runbook.md`, `docs/persistent_collection_plan.md`,
`docs/price_history.md`, `docs/custody_flow.md`, `src/fintr/collector/`,
`src/fintr/data/models.py`, `src/fintr/data/period_capture.py`.
