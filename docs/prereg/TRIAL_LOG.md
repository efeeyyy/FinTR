# Deneme defteri (trial log)

**Durum: docs'tan yeniden kuruldu, eksik olabilir.** Bu defter yalnız Git'te izlenen belgelerden
(`DECISIONS.md`, `FREEZE.md`, `README.md`, `H1`–`H4.md`, `docs/STATUS.md`, `docs/research/*`) ve
`git log`'dan derlendi. `data/` altına bakılmadı; hiçbir çalışma sonucu, p-değeri, etki büyüklüğü
veya getiri okunmadı ya da buraya yazılmadı. Belgelerin söylemediği her şey "unknown" (bilinmiyor)
olarak işaretlidir. Bu defter bir avantaj (edge) iddiası değildir.

## Amaç

Geliştirme verisine bakılan her analizi, denenen her hipotez/eşik varyantını ve her çalışma
koşusunu tek yerde saymak. Gerekçe: Bailey/López de Prado'nun deflated Sharpe ve minimum backtest
uzunluğu yaklaşımları ancak deneme sayısı bilinirse uygulanabilir
(`docs/research/signal_bot_best_practices_2026-10-13.md` bölüm A1).

## Kurallar (gelecek kayıtlar için)

1. **Yalnız ekleme (append-only).** Eski satır silinmez; düzeltme yeni satırla yapılır.
2. Geliştirme verisinde yapılan her yeni analiz, her yeni hipotez/eşik varyantı, her çalışma
   koşusu ve değerlendirilen her LLM istem (prompt) sürümü değişikliği için **sonuç okunmadan
   önce** bir satır eklenir.
3. Her satır: tarih, ne denendi/bakıldı, hangi veri (dev/train/holdout), sonuç değerleri okundu mu
   (evet/hayır/unknown), karar, kaynak (dosya:satır veya commit).
4. Holdout testi satırı, izin verilen **tek** test koşusudur (`DECISIONS.md` 2026-09-28; tek sefer).
5. **K4 hatırlatması:** 2027-03-31'e kadar ara performans değerlendirmesi yoktur.
6. Dondurulmuş spec dosyaları ve hash'leri (`FREEZE.md`) değişmez; varyant = yeni sürüm + yeni satır.

## Sayım özeti (docs'tan yeniden kuruldu, eksik olabilir)

| Kalem | Sayı | Not |
|---|---|---|
| Dondurulan hipotez | 4 (H1, H2 birincil; H3, H4 ikincil) | `FREEZE.md:6-14` |
| Dondurma öncesi eşik/hipotez varyantı | 1 set (0,01 / ±0,10 / top1 >= 0,10), a priori; 1 alternatif (yüzdelik tabanlı eşik) önerildi, kullanılmadı | `README.md:262-267`, `DECISIONS.md:14-15` |
| Vade sayısı (hipotez başına) | 4 (1, 5, 10, 20; birincil 5) | `README.md:70` |
| Spec'lenmeyen aday özellik fikirleri | "Yeni feature gerekiyor" listesinde (N1..N9) | `README.md:120-135` |
| Train koşusu (H1–H4, 20 dev hissesi) | en az 4 (hipotez başına 1); yeniden koşu sayısı unknown | `README.md:34`, `STATUS.md:49,56` |
| Tarama (scanner) koşusu, as_of 2026-09-25 | 2 (normal ve son 5 gün) | `STATUS.md:30` |
| Holdout test koşusu | 0 (planlı: 1) | `DECISIONS.md:39-48` |
| Değerlendirilen LLM istem sürümü | v1, v2, v3 (+ probe v2; "kazanç" yasağı eklendi) | T-17..T-20 |

## Defter

| ID | Tarih | Ne denendi / bakıldı | Veri | Sonuç değerleri okundu mu | Karar | Kaynak |
|---|---|---|---|---|---|---|
| T-01 | 2026-09-26 | H1–H4 önerileri yazıldı; eşikler, vadeler a priori seçildi | yok (veri görülmedi) | hayır | öneri, donmamış | `22b9cc9`, `README.md:11-14` |
| T-02 | 2026-09-26 | Geliştirme materyali kaydı (AKFIS dönem kayıtları) ve ilk dönem raporları | dev | unknown | geliştirme materyali olarak kayıt | `916bc36`, `STATUS.md:28` |
| T-03 | 2026-09-26 | retail_pct yöntemi tek hissede doğrulandı (hisse adı belgede yok) | dev | unknown (yalnız "tutarlı" notu var) | yöntem kabul | `STATUS.md:29` |
| T-04 | 2026-09-27 | Dondurma öncesi veri kalitesi: AKD-türevi lot / Yahoo hacim oranı, 20 dev hissesi | dev | hayır, getiri okunmadı (yalnız oran) | ölçek rejimi günleri düşülür | `DECISIONS.md:25-37` |
| T-05 | 2026-09-27 | Efe kararları: eşikler sabit, H3/H4 ikincil, vade 5, yüzdelik eşik seçilmedi | yok | hayır | karar | `DECISIONS.md:3-23` |
| T-06 | 2026-09-27 | Motor: ratio-regime günlerini düşürme (E1/E4 ön koşulları) | dev (kod) | hayır (belgeye göre) | birleşti | `71d8c58`, `855778e` |
| T-07 | 2026-09-27 | H1–H4 dondurma, spec hash'leri | dev, getiri okunmadan | hayır (`FREEZE.md:3-4`) | donduruldu | `d3f44a3`, `FREEZE.md` |
| T-08 | 2026-09-27 sonrası (kesin tarih unknown) | Train koşusu H1: `run_study.py --split train --cost-bps 10 --save` | train (20 dev) | 2026-10-02 itibarıyla yönetici oturumu okumamış; sonrası unknown | keşif örneği; spec değişmez | `README.md:34-40`, `DECISIONS.md:91`, `STATUS.md:28`, `383aa1a` |
| T-09 | aynı | Train koşusu H2 | train | aynı | aynı | aynı |
| T-10 | aynı | Train koşusu H3 | train | aynı | aynı | aynı |
| T-11 | aynı | Train koşusu H4 | train | aynı | aynı | aynı |
| T-12 | 2026-09-27/28 (unknown) | Tarama as_of 2026-09-25 (scanner) | dev | "okunmadı, finans sohbetinde" (`STATUS.md:30`); sonrası unknown | gözlem aracı, getiri yok | `STATUS.md:30` |
| T-13 | aynı | Tarama as_of 2026-09-25, son 5 gün (scanner_lb5) | dev | aynı | aynı | `STATUS.md:30` |
| T-14 | 2026-09-28 | Train çıktılarının yapısal karşılaştırması (test planı birleşiminden sonra "train sonuçları değişmedi"); yeniden koşu olup olmadığı unknown | train | unknown (değer okuma belirtilmemiş) | sonuç değişmedi | `STATUS.md:55-56` |
| T-15 | 2026-09-28 | Test kapsamı kararı (30 sembol birincil, 10 holdout ikincil, tek sefer) | yok (test verisi yok) | hayır | karar | `DECISIONS.md:39-48`, `90d561f` |
| T-16 | 2026-09-28 | Holdout fiyat geçmişi yalnız veri kalitesi tespiti için kullanılır | holdout fiyat (veri kalitesi) | hayır | istisna kaydı | `DECISIONS.md:50-64` |
| T-17 | 2026-10-02 | LLM analiz notu: istem v1 (Opus 5.5 kanıt değerlendirmesi) | dev gözlemleri (günlük) | unknown | varsayılan kapalı, doğrulayıcı fail-closed | `DECISIONS.md:102-156`, `c1d8c80` |
| T-18 | 2026-10-03 | İstem v2 (1..6 gerekçe, sayma sözcüğü yok), canary tabanlı izolasyon probe'u, ilk gerçek not koşusu | dev gözlemleri | unknown | birleşti | `b4e90b2`, `e096200`, `cd9ee93` |
| T-19 | 2026-10-03 | İstem v3: TRALT notu iki kez `forbidden_phrase: getiri` ile reddedildi | dev gözlemi (TRALT) | unknown (red kategorisi biliniyor) | v3 eklendi | `DECISIONS.md:196-201`, `a7009b1` |
| T-20 | 2026-10-05 | Yasak sözcük listesine "kazanç" eklendi, max_run_seconds 1800 | yok | hayır | birleşti | `9fc1aa5`, `af7c47c` |
| T-21 | 2026-10-01 sonrası, sürüyor | Günlük toplama/tarama/bildirim koşuları (dev hisseleri, test penceresi tarihleri); bildirim getiri göstermez | dev hisseleri, ileriye dönük | gözlem satırları gönderilir; getiri/performans değerlendirmesi yok (K4) | sürüyor; her koşu run-record'da | `STATUS.md:94,175-178`, `DECISIONS.md:66-100` |
| T-22 | 2026-10-02/03 | Bildirim düzeni v1/v2/v3 ve yön etiketi (sunum değişikliği, istatistik değil) | yok | hayır | Efe kararı | `DECISIONS.md:66-100,158-195` |
| T-23 | 2026-10-05 | Sentetik holdout günü provası (fixture, gerçek veri yok) | sentetik | hayır | birleşti | `a4e12d1`, `2dd0887` |
| T-24 | 2026-10-09/10 | Yahoo geçmiş fiyat dosyalarının eski/yeni karşılaştırması (tatil/dolgu satırları, adjusted_close revizyonu), 20 dev hissesi; salt okunur | dev (train penceresi tarihleri) | veri kalitesi bulgusu okundu; getiri yok | açık: train etkisi değerlendirilmedi | `STATUS.md:178-190` |
| T-25 | 2026-10-13 | Sinyal botu doğrulama pratikleri araştırma notu | yok | hayır | öneri (bu defter = A1) | `d1fdaf2`, `docs/research/signal_bot_best_practices_2026-10-13.md:19-23` |
| T-26 | planlı (2027 Nisan) | **Holdout testi**, birincil + ikincil kapsam; izin verilen tek test koşusu | test | henüz çalışmadı | beklemede | `DECISIONS.md:39-48`, `README.md:41-57` |
| T-27 | 2026-10-13 | Motorun sentetik kalibrasyonu (A4): sentetik panelde (20 sembol x 250 gün, rastgele yürüyüş fiyat, sentetik AKD/takas/metadata) boş hipotez ve yerleştirilmiş etki altında dondurulmuş H1/H2 spec'leri gerçek `run_study` + Holm ile koşuldu; spec, eşik ve motor değişmedi | sentetik (gerçek veri, gerçek getiri veya holdout yok) | hayır: gerçek bir sonuç/p-değeri okunmadı; çıktı yalnız motorun yanlış-pozitif oranı ve gücüdür, piyasalar hakkında değildir | çalıştırma yarıda (1316/2300 replikat; ısınma nedeniyle durduruldu), devam edecek (`--resume`); bulgular tamamlanınca `docs/research/engine_calibration.md`'de; motor/spec düzeltmesi gerekirse `DECISIONS.md` kaydı Efe kararıdır | `scripts/calibrate_engine.py`, `tests/synthetic_market.py`, `docs/research/signal_bot_best_practices_2026-10-13.md:40` |
| T-28 | 2026-10-14 | Sentetik kalibrasyonun `market_share` (0,4) varsayımını sınamak için gerçek getirilerden yalnız ortak hareket ölçüsü: 20 geliştirme hissesinin günlük kapanış-kapanış log getirileri, train penceresi 2025-09-26..2026-09-25 (fiyat dosyası bitişi 2026-09-25), yer tutucu/boş barlar düşülür; ortalama ikili korelasyon ve eşit ağırlıklı piyasa faktörünün açıkladığı varyans payı (diğer hisselerden). Getiri ortalaması, sinyal, koşul veya çalışma sonucu hesaplanmaz | dev/train (holdout yok) | evet: yalnız korelasyon/varyans payı okunur | sentetik senaryoya ek `market_share` değeri gerekip gerekmediğine karar; spec değişmez | bu satır; çıktı `data/interim/research/market_share_2026-10-14.json` |

## Belirsizlikler

- Train koşularının tam tarihi, yeniden koşu sayısı ve sonuçların sonradan okunup okunmadığı
  belgelerde yok; `STATUS.md:28,30` yalnız "okunmadı, finans sohbetinde" der.
- T-02/T-03 için hangi hissede ve ne kadar bakıldığı belgelerden çıkmaz.
- LLM istem koşularının hangi hisse/tarihlerde yapıldığı `data/interim/llm_notes/` altındadır;
  bu defter oraya bakmadı.
