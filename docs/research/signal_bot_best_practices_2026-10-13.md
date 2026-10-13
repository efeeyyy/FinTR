# Sinyal botu / nicel araştırma yapanlar doğru sonuç için ne yapıyor? FinTR-LLM'e uyarlama

Tarih: 2026-10-13. Bu belge bir literatür ve pratik taramasıdır (alt ajan, web araması). Projenin
sonuçları hakkında hiçbir iddia içermez ve yatırım tavsiyesi değildir.

Kaynak işaretleri:
- **[özet]**: kaynak yalnızca web arama sonucu özetinden görüldü, tam metin okunmadı. Rakamları kullanmadan önce asıl kaynaktan teyit et.
- **[sayfa]**: sayfa WebFetch ile açıldı, ama yalnız kısa bir özet alındı. Tam makale değil.
- **[belirsiz]**: doğrulanamadı veya kaynak bulunamadı.

"Çakışma": dondurulmuş spec'e/ön kayda dokunmak veya holdout (2026-10-01..2027-03-31) performansına bakmak = çakışma VAR.

"Bizde" yargıları `docs/` dosyaları ve birkaç grep üzerinden verildi. Kod derin okunmadı.

---

## A. Aşırı uyum (overfitting) ve çoklu test

### A1. Deneme sayısını kaydetmek (trial log) ve ona göre ceza uygulamak
- **Ne:** Kaç fikir/parametre denendiğini kaydetmek. Çok deneme yapılınca şans eseri iyi görünen sonuç çıkar. Bailey ve López de Prado "Deflated Sharpe Ratio" ile deneme sayısına göre düzeltir. "Minimum Backtest Length" N deneme için gereken asgari geçmiş uzunluğunu verir. İkisi de ancak her deneme kaydedilirse çalışır.
- **Kaynak [özet]:** [Deflated Sharpe Ratio (SSRN 2460551)](https://papers.ssrn.com/abstract=2460551); [Pseudo-Mathematics and Financial Charlatanism](https://scholarworks.wmich.edu/math_pubs/40/); [Probability of Backtest Overfitting](https://papers.ssrn.com/abstract=2326253)
- **Bizde:** Kısmen. Ön kayıt + hash var (`docs/prereg/FREEZE.md`), aile m=2 (`docs/prereg/DECISIONS.md` 2026-09-27). Ama dondurmadan önce kaç hipotez/eşik denendiğinin ve geliştirme verisine kaç kez bakıldığının tek bir defteri yok.
- **Öneri:** `docs/prereg/TRIAL_LOG.md`: tarih, ne denendi, hangi veriyle, sonucu okundu mu, karar. Dondurma sonrası her yeni analiz de buraya.
- **Efor:** küçük. **Çakışma:** yok.
- Not: PBO/CSCV ve DSR formülleri çok sayıda aday strateji içindir. Bizde 4 dondurulmuş kural var, bu yüzden formül yerine "deneme sayısı şeffaflığı" ilkesi alınır.

### A2. Aile bazlı çoklu karşılaştırma düzeltmesi (Holm / FDR / yüksek t eşiği)
- **Ne:** Çok hipotez test edilince p<0,05 yetmez. Harvey-Liu-Zhu yeni bir faktör için t>3 civarı eşik savunur. Harvey-Liu "Sharpe haircut" önerir.
- **Kaynak [özet]:** [Harvey, Liu, Zhu (NBER w20592)](https://nber.org/papers/w20592); [Harvey & Liu "Backtesting"](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF). Karşı görüş: [arXiv 2206.15365](https://arxiv.org/pdf/2206.15365).
- **Bizde:** Zaten yapıyoruz. Holm m=2 (H1+H2), H3/H4 ikincil (`docs/prereg/README.md:75`).
- **Öneri:** Yok. İsteğe bağlı: rapora "bu sonuç kaç hipotezden biri" cümlesi (A1 defteriyle).

### A3. Veri madenciliği testleri: White Reality Check, Hansen SPA, Sullivan-Timmermann-White
- **Ne:** Çok sayıda kural arasından en iyisi seçilince, "bu kural gerçekten kıyas modelinden iyi mi?" sorusunu tüm aday kümesi üzerinden bootstrap ile cevaplar.
- **Kaynak [özet]:** [White 2000](https://users.ssc.wisc.edu/~bhansen/718/White2000.pdf); [Hansen SPA üzerine (Brown)](https://economics.brown.edu/sites/g/files/dprerj726/files/papers/2003-9_paper.pdf); [Sullivan, Timmermann, White 1999](https://ideas.repec.org/p/fmg/fmgdps/dp303.html)
- **Bizde:** Yapmıyoruz, gerek de yok: kural seçimi yok, 4 kural önceden donduruldu, sinyal tarihine göre blok bootstrap var (`docs/prereg/README.md:74`).
- **Öneri:** v2 ailesi çok sayıda aday kuralla kurulursa SPA/RC şart koşulur.
- **Efor:** orta (ileride). **Çakışma:** şimdi eklenirse VAR (yeni analiz), yalnız v2 için.

### A4. Motorun kendisini "boş hipotez" altında sınamak (null kalibrasyon / plasebo)
- **Ne:** Sinyalin sıfır bilgi taşıdığı veriyle (rastgele yürüyüş veya karıştırılmış sinyal günleri) tüm hattı çalıştırıp yanlış-pozitif oranının beklenen düzeyde çıktığını doğrulamak. Bootstrap/Holm uygulaması hatalıysa bunu yakalar.
- **Kaynak [belirsiz]:** Genel mühendislik pratiği; tek bir birincil kaynak yok (STW bootstrap'ı boş dağılım için kullanır).
- **Bizde:** Yapmıyoruz (doğrulanmadı). `tests/test_study_prereqs.py` motor yeteneklerini sınıyor; yanlış-pozitif oranı kalibrasyonu görülmedi.
- **Öneri:** Yalnız sentetik veriyle `run_study` motorunu yüzlerce kez çalıştıran bir test betiği: sahte hisse/fiyat/AKD üret, koşullu etkiyi sıfır tut, p<0,025 oranını ölç. Sonra bilinen bir etki ekleyip gücü ölç ve `docs/prereg/README.md:227` güç hesabıyla karşılaştır.
- **Efor:** orta. **Çakışma:** yok (sentetik veri).

---

## B. Veri doğruluğu: look-ahead, point-in-time, revizyonlar

### B1. Ham veriyi tarihli snapshot olarak saklamak, üzerine yazmamak
- **Ne:** Sağlayıcı geçmişi sonradan değiştirebilir. Ham yanıt + erişim zamanı + hash saklanır, değişiklik yeni snapshot olur.
- **Kaynak [özet]:** [PIT veri ve revizyon sorunu](https://permutable.ai/point-in-time-datasets-for-macro-backtesting/); Yahoo düzeltilmiş fiyat raporları: [apify](https://apify.com/superhero99999/yahoo-finance-ohlcv), [tradingqna](https://tradingqna.com/t/has-anyone-found-wrong-adjusted-prices-in-nse-historical-data-for-backtesting/192938). Resmî Yahoo açıklaması bulunamadı [belirsiz].
- **Bizde:** Zaten yapıyoruz: `raw_response`, `normalized_sha256`, no-overwrite (`docs/price_history.md`); yarım dosyalar `_incomplete/`'e taşınıyor.
- **Boşluk:** Revizyonu kendiliğinden yakalayan bir şey yok; 2026-10-10 fark raporu elle yapıldı (`data/interim/research/yahoo_diff_2026-10-09/report.md`).
- **Öneri (B1a):** `scripts/diff_price_history.py`: haftalık son N günü geçici dosyaya yeniden çek, kayıtlı dosyayla karşılaştır, ham OHLCV / `adjusted_close` / satır kümesi değişimini raporla; eski dosyaya dokunma; eşik üstünde "veri" uyarısı.
- **Efor:** küçük-orta. **Çakışma:** yok.

### B2. Ham fiyat ve kurumsal işlem tablosunu ayrı tutmak; ikinci kaynakla çapraz kontrol
- **Kaynak [özet]:** B1'deki Yahoo kullanıcı raporları.
- **Bizde:** Kısmen. Ham `close` ve `adjusted_close` ayrı; kurumsal işlem altyapısı var (`docs/corporate_actions.md`) ama `configs/corporate_actions/` boş; AKD lot / Yahoo hacim ölçek rejimi tespiti var (DECISIONS 2026-09-27).
- **Öneri:** KAP belgelerinden kurumsal işlem tablosunu doldur; örneklemde ikinci kaynakla çapraz kontrol. Yeni dışlama kuralı getiri okumadan DECISIONS'a yazılır.
- **Efor:** orta. **Çakışma:** yok (testten önce kayda geçerse).

### B3. Lookahead için otomatik "kısalt ve karşılaştır" testi
- **Ne:** Freqtrade `lookahead-analysis`: tam backtest ile kısaltılmış verili backtestleri karşılaştırır; değerler değişiyorsa gelecek veri kullanılıyordur.
- **Kaynak [sayfa]:** [Freqtrade lookahead-analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
- **Bizde:** Büyük ölçüde var: tarayıcıda `LookaheadTests` (`docs/scanner.md`), `truncate_period_capture`. `fintr.research.features` için genel test doğrulanmadı.
- **Öneri:** Tüm özellik/koşul fonksiyonları için tek bir özellik tabanlı test: veriyi t'de kes, t'deki değer tam veriyle aynı olmalı.
- **Efor:** küçük. **Çakışma:** yok.

### B4. Yayın gecikmesi ve tazelik
- **Bizde:** Zaten yapıyoruz (tazelik raporu, `custody_flagged_stale_t_plus_2`, kesinleşmemiş gün sayımı).

### B5. Hayatta kalma yanlılığı / geçmiş evren üyeliği
- **Kaynak [özet]:** [dev.to özeti](https://dev.to/tradevodata/survivorship-bias-vs-lookahead-bias-the-two-silent-backtest-killers-pmm), [CFA notu](https://analystprep.com/study-notes/cfa-level-2/problems-in-backtesting/)
- **Bizde:** Kısmen. Dönemlik evren dosyaları var; `docs/prereg/README.md` ~210'da "geriye dönük üyelik doğrulanmadı" notu.
- **Öneri:** Evren dosyalarının kaynağını belgele; doğrulanmamışsa raporda açıkça belirt. **Efor:** küçük. **Çakışma:** yok.

### B6. Tatil / işlem günü takvimi
- **Bizde:** Yok (repo'da tatil takvimi yok; yer tutucu satırlar Yahoo'nun kendisine dayanıyor).
- **Öneri:** Borsa İstanbul resmî tatil listesinden `configs/calendar/bist_holidays.json` (kaynak URL, hash, erişim zamanı); "iş günü ama bar yok/dolgu" ve "tatil ama normal bar" uyuşmazlıklarını raporla. Tarihler uydurulmaz, Efe resmî kaynaktan doğrular.
- **Efor:** küçük-orta. **Çakışma:** yok (düşülen günleri değiştirirse DECISIONS kaydı).

---

## C. Gerçekçi maliyet ve işlem koşulları

### C1. Spread, kayma, piyasa etkisi
- **Kaynak [özet]:** [Novy-Marx & Velikov (NBER w20721)](https://www.nber.org/papers/w20721); [Bucci ve ark.](https://arxiv.org/pdf/1811.05230); [Brokmann ve ark.](https://ar5iv.arxiv.org/html/1407.3390); [QuantConnect slippage](https://quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models)
- **Bizde:** Kısmen. Taraf başına 10 bps sabit varsayım (`docs/prereg/README.md:71`); spread/etki modeli yok.
- **Öneri:** Maliyet duyarlılık tablosu (5/10/20/40 bps), ızgara holdout görülmeden DECISIONS'a yazılır. Gerçek spread için emir defteri gerekir (yok).
- **Efor:** küçük (tablo) / büyük (gerçek spread). **Çakışma:** kısmi (önceden kayıt şart; spec değişmez).

### C2. Fiyat limitleri (tavan/taban)
- **Kaynak [özet]:** [Are Price Limits Effective? ISE](https://ideas.repec.org/a/bla/jfnres/v29y2006i3p383-403.html); [Aktas, Kryzanowski, Zhang](https://ideas.repec.org/a/eme/imefmp/imefm-04-2020-0151.html); [BIST 2020 fiyat limitleri](https://borsaistanbul.com/files/2020-20-Daily-Price-Limits.pdf); [BIST Pay Piyasası](https://borsaistanbul.com/en/investment-guide/markets/equity-market). Güncel limit yüzdesi ve 2025-08-29 devre kesici değişikliği [belirsiz].
- **Bizde:** Yok (kodda limit günü işareti bulunamadı).
- **Öneri:** Betimleyici "limit günü" etiketi ve sayımı; olay dışlamaz. Limit kuralı tarih bazında resmî kaynaktan kayda geçer.
- **Efor:** küçük-orta. **Çakışma:** yok (yalnız betimleyici).

---

## D. Doğrulama tasarımı

### D1. Ön kayıt, spec dondurma, tek seferlik holdout
- **Kaynak [özet]:** Quantopian 888 algoritma: backtest Sharpe'ı örneklem dışını zayıf tahmin ediyor (R² < 0,025): [Quantpedia özeti](https://quantpedia.com/quantopians-academic-paper-about-in-vs-out-of-sample-performance-of-trading-alg/); [10 Reasons Most ML Funds Fail](https://www.garp.org/white-paper/the-10-reasons-most-machine-learning-funds-fail).
- **Bizde:** Zaten yapıyoruz (hash'li spec, test planı, O_EXCL kilit, K4).

### D2. Purged / embargolu CV ve walk-forward
- **Kaynak [özet]:** [Purged CV](https://en.wikipedia.org/wiki/Purged_cross-validation); [skfolio CPCV](https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html)
- **Bizde:** Model eğitimi yok, uygulanamaz. İlgili nokta: train olaylarının 5 günlük getiri penceresi test başlangıcına taşmamalı (`docs/prereg/README.md:143`, doğrulanmadı).
- **Öneri:** Birim testi "hiçbir train olayının çıkış tarihi test_start'tan sonra değil". **Efor:** küçük. **Çakışma:** yok.

### D3. İleri test (shadow mode) ve tahminleri sonuçtan önce değiştirilemez kaydetmek
- **Kaynak [özet]:** [McLean & Pontiff](https://ivey.uwo.ca/media/3775549/pontiff.pdf) (yayın sonrası düşüş; rakamlar sürüme göre farklı). Kuluçka için birincil kaynak yok [belirsiz].
- **Bizde:** Büyük ölçüde (günlük observations, receipt, kural hash'i, `code_commit`; K4 kilidi).
- **Boşluk:** "Sonuçtan önce yazıldı" kanıtı yerel dosya zamanına bağlı.
- **Öneri:** Günlük observations/receipt SHA-256'larını `forward_log/manifest.jsonl` hash zincirine ekle, düzenli git commit/tag. İsteğe bağlı harici zaman damgası (OpenTimestamps benzeri) [belirsiz].
- **Efor:** küçük. **Çakışma:** yok.

---

## E. Canlı izleme

### E1. Veri kalitesi testleri ve alarmlar
- **Kaynak:** [ML Test Score](https://research.google/pubs/the-ml-test-score-a-rubric-for-ml-production-readiness-and-technical-debt-reduction/) [sayfa]; [Great Expectations](https://docs.greatexpectations.io/docs/1.0-prerelease/core/introduction/about_gx), [pandera](https://pypi.org/project/pandera/0.20.0/) [özet]
- **Bizde:** İyi bir bölümü var (ölçek sıçraması, ratio-regime, no-new-data, null-close guard, tazelik, çalıştırma kaydı, K6).
- **Boşluk:** Tek bir günlük sağlık özeti ve kaçırılan gün alarmı yok (10-05 elle fark edildi).
- **Öneri:** Pipeline sonunda sağlık satırı (beklenen gün, tazelik, şema hash'i, boş/dolgu bar sayısı); eşikte ayrı Telegram uyarısı. **Efor:** orta. **Çakışma:** yok.

### E2. Sinyal bozunması ve drift
- **Bizde:** Performans cinsinden YAPILMAMALI (K4).
- **İzinli yan yol:** Getirisiz girdi-dağılımı izleme (koşul sağlayan hisse sayısı, `retail_pct` dağılımı, bastırma oranı, veri boşluğu). **Efor:** orta. **Çakışma:** performans izleme VAR; girdi-frekansı yok.

### E3. Backtest–canlı uzlaştırma / yeniden oynatma
- **Kaynak [belirsiz]:** genel ilke.
- **Bizde:** Kısmen (`scripts/compare_daily_d0.py`, deterministik yapı, `code_commit`).
- **Öneri:** Haftalık replay: son 5 as_of için tarayıcıyı saklı veriden yeniden çalıştır, saklı `observations.json` ile karşılaştır; fark = alarm. **Efor:** küçük. **Çakışma:** yok.

---

## F. LLM bileşeni

### F1. Sayı ve yasak ifade denetimi, şemalı çıktı
- **Kaynak [özet]:** [FinanceBench](https://hyper.ai/en/papers/2311.11944), [Patronus FinanceBench](https://docs.patronus.ai/docs/research_and_differentiators/financebench), [PHANTOM (NeurIPS 2025)](https://papers.nips.cc/paper_files/paper/2025/hash/b8badadce3f482ba340ff870f4894441-Abstract-Datasets_and_Benchmarks_Track.html)
- **Bizde:** Zaten yapıyoruz (`src/fintr/llm_note/numbers.py`, `forbidden.py`, `validate.py`). `docs/architecture.md` (~942) rakam denetiminin yorum doğruluğunu yakalamadığını kabul ediyor.
- **Öneri:** (a) Altın/negatif örnek seti (bozuk notlar validator'dan geçmemeli; regresyon testi). (b) Notların %5-10'unu Efe'nin elle "gerekçe girdiden çıkar mı" etiketlemesi; bu etiketlerle holdout dönemi verisinde prompt ayarı yapılmaz.
- **Efor:** küçük-orta. **Çakışma:** yok.

### F2. LLM ezberlemesi / öngörü yanlılığı
- **Kaynak [sayfa: özet]:** [Gao, Jiang, Yan (arXiv 2512.23847)](https://arxiv.org/abs/2512.23847); [FinCAD (arXiv 2605.24564)](https://arxiv.org/abs/2605.24564). Özetteki oranlar doğrulanmadı.
- **Bizde:** Politika yok.
- **Öneri:** LLM not kelimesi hiçbir istatistiksel çalışmaya girdi olmaz; geri doldurulan notlar işaretlenir; as_of < model bilgi kesimi için ek işaret (kesim tarihi teyit edilmeli). **Efor:** küçük. **Çakışma:** yok.

---

## G. BIST'e özgü noktalar

- **T+2 ve olası T+1 [sayfa]:** Borsa İstanbul 2025-05-05'te T+1 fizibilitesini başlattı; uygulama kararı bulunamadı: [duyuru](https://borsaistanbul.com/en/announcement/14796/borsa-istanbul-initiates-evaluation-process-shortening-equity-market-settlement-cycle-t2-t1). Takas gecikmesi kodda sabit (`src/fintr/scanner/freshness.py:20`, `CUSTODY_STALE_SERVER_DATE_GAP = 2`; yönetici doğruladı). Öneri: tarihe bağlı yapılandırma (küçük); holdout içinde geçiş olursa DECISIONS notu.
- **Devre kesici / limit kuralları zamanla değişiyor [özet]** (2025-08-29 duyurusu okunmadı); kural sürümü tarihle kaydedilmeli (C2).
- **AKD/takas literatürü [özet]:** hakem denetimli AKD/takas çalışması bulunamadı; ilgili: [Tiniç (Bilkent tezi)](https://repository.bilkent.edu.tr/items/8a63d863-4038-45ea-8cb1-6640ad472143); [BIST market data ürünleri](https://borsaistanbul.com/en/data/data-dissemination/market-data-products). AKD kullanım izni Efe beyanı, doğrulanmadı.
- **Replikasyon [özet]:** [Hou, Xue, Zhang](https://www.nber.org/papers/w23394.pdf) ve [Chen & Zimmermann](https://www.federalreserve.gov/econres/feds/open-source-cross-sectional-asset-pricing.htm) zıt görünen bulgular bildiriyor; beklenti yönetimi notu.

---

## Öncelikli ilk 8

| # | Uygulama | Bizdeki boşluk | Efor | Prereg/K4 çakışması |
|---|---|---|---|---|
| 1 | Motoru sentetik boş-hipotez verisiyle kalibre et: yanlış-pozitif ve güç (A4) | Bootstrap/Holm hattı kalibre edilmedi (doğrulanmadı) | orta | Hayır |
| 2 | Otomatik veri revizyonu fark izleme + resmî tatil takvimi (B1a, B6) | Revizyon elle bulundu; takvim yok | küçük-orta | Hayır |
| 3 | Tahmin günlüğü hash zinciri + düzenli git etiketi (D3) | Kanıt yerel zaman damgasına bağlı | küçük | Hayır |
| 4 | Deneme/analiz defteri TRIAL_LOG (A1) | Bakılanlar tek yerde değil | küçük | Hayır |
| 5 | Haftalık replay uzlaştırması (E3) | Kod/veri kayması sessiz kalabilir | küçük | Hayır |
| 6 | LLM: backfill politikası, altın/negatif validator seti, elle sadakat örneklemi (F1, F2) | Anlamsal sadakat ölçülmüyor | küçük-orta | Hayır |
| 7 | Günlük veri sağlığı özeti + heartbeat + getirisiz girdi izleme (E1, E2) | Kaçırılan gün elle fark ediliyor | orta | Hayır (performans izleme yasak) |
| 8 | Maliyet duyarlılık tablosu + tavan/taban işareti, holdout öncesi DECISIONS'a yazılı (C1, C2) | Tek maliyet varsayımı; limit günleri bilinmiyor | küçük-orta | Kısmi |

Ek (küçük): T+2'yi yapılandırmaya taşı (G), özelliklerde "kısalt ve karşılaştır" testi (B3), train-test örtüşme testi (D2), evren üyeliği kaynak notu (B5), kurumsal işlem tablosu (B2).

## Sınırlamalar
- Kaynakların çoğu arama özetinden; rakamlar (t>3, %58/%26, R²<0,025 vb.) kullanılmadan önce teyit edilmeli.
- BIST güncel limit / devre kesici eşikleri ve 2025-08-29 değişikliği doğrulanmadı.
- "Yapıyoruz/yapmıyoruz" yargıları docs ve kısa grep üzerinden; kod doğrulaması yapılmadı (A4, B3, D2, D3).
- Hiçbir proje sonucu okunmadı veya değerlendirilmedi.
