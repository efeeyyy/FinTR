# Ön kayıtlı araştırma hipotezleri — BIST 30 AKD/fiyat ailesi v1

**Durum: ÖNERİ — Efe'nin onayını bekliyor; tavsiye değildir.** Bu klasördeki
hipotezler ve `configs/studies/prereg/` altındaki eşleşen spec'ler
`"frozen": false` durumundadır. Hiçbiri doğrulanmış bir bulgu, al/sat sinyali,
bir şirket/kurum hakkında niyet veya manipülasyon iddiası değildir. Sonuçlar
yalnız "koşul günlerinin ardından gelen tarihsel ileri getiri ortalaması,
aynı sembollerin tüm günlerinin ortalamasından farklı mıydı" sorusuna
istatistiksel yanıt verir.

Tasarım **hiçbir piyasa verisine bakılmadan** yapıldı: bu ortamda `data/`
yoktur ve eşikler, vadeler, olay sayısı hedefleri önceden (a priori)
seçildi. Aşağıdaki güç hesabındaki oynaklık/korelasyon rakamları veriden
değil, açıkça işaretlenmiş varsayımlardır.

## Süreç

1. **Öneri (şimdi).** Bu belgeler + `"frozen": false` spec'ler. Değişiklik
   serbesttir ama yalnız veri görülmeden.
2. **Efe onayı.** Efe hipotezleri, eşikleri, birincil vade/test ve çoklu
   karşılaştırma planını onaylar ya da değiştirir. Aşağıdaki
   "Efe'nin kararları" bölümü açık soruları listeler.
3. **Dondurma.** Onaydan sonra her spec'te `"frozen": true` ve
   `preregistration.status: "frozen_approved_by_efe"` yapılır, `spec_sha256`
   (`fintr.research.study_spec.load_study_spec(...).spec_sha256`) bu README'ye
   tabloya yazılır ve tek bir commit'le kaydedilir. Dondurmadan sonra spec'in
   tek baytı değişirse yeni sürüm (`..._v2`) ve yeni kimlik gerekir; önceki
   sürümün görülmüş sonuçları varsa bu açıkça belirtilir.
4. **Motor ön koşulları.** Train çalıştırılmadan önce E1 (birincil test) ve
   E4 (çoklu capture birleştirme) motor yetenekleri uygulanıp test edilir
   (bkz. "Motor yetenekleri"; `engine-prereqs` dalında uygulandı,
   `tests/test_study_prereqs.py`). Motorun i.i.d. bootstrap güven aralığı
   yalnız ikincil tanılamadır; tek başına doğrulayıcı sonuç sayılmaz.
5. **Train (geliştirme hisseleri).** `scripts/run_study.py --split train
   --cost-bps 10 --save`; yalnız `configs/splits/bist30_2026_holdout_v2.json`
   geliştirme hisseleri (20 sembol), sinyal tarihleri `< 2026-10-01`, fiyat
   dosyası `2026-09-30`'da biter (train getirileri test dönemi fiyatlarına
   taşmaz; son sinyaller `insufficient_bars` olarak düşer). Sonuçlar
   raporlanır, **spec'i değiştirmek için kullanılmaz**. Train aşaması keşif
   /geliştirme örneğidir, doğrulayıcı değildir.
6. **Tek seferlik holdout testi.** Tüm dört hipotez, train sonucundan
   bağımsız olarak (etkiye göre eleme yok) test aşamasına gider:
   `--split test --test-plan configs/studies/prereg/test_plan_v1.json
   --test-scope primary|secondary` (bkz. "Holdout testi çalıştırma" altında).
   **Karara bağlandı** (`DECISIONS.md` 2026-09-28, E5 ile birlikte):
   **birincil** test motorun `--split test` tanımını kullanır (sinyal
   tarihleri `[2026-10-01, 2027-03-31]`, tüm bildirilen 30 sembol = 20
   geliştirme + 10 holdout, `symbol_scope="declared"`); Holm (m=2, H1+H2)
   yalnız bu sonuçlara uygulanır. `holdout-guard` dalıyla gelen CLI
   koruması (yalnız holdout sembolleri, `symbol_scope="holdout_only"`;
   bkz. `docs/study_engine.md`) **ikincil** (E5, sağlamlık) kapsam olarak
   korunur ve tek başına doğrulayıcı değildir. Test dönemi **ileriye
   dönük** toplanır: sinyal penceresi `2026-10-01..2027-03-31`, fiyat
   verisi pencere sonundan en az 25 tradeable bar sonrasına kadar (E3,
   motorda zorlanır: `engine.signal_dates_for_split`'in `window_end`'i).
   Test bir kez çalıştırılır; sonuç ne olursa olsun raporlanır.

Holdout sembollerinin `2026-10-01` öncesi verisi hiçbir aşamada kullanılmaz
(motorun train split'i onları zaten dışlar). `data/eval/` ve
`src/fintr/evaluation/dev_registry.json` kayıtları (ör. AKFIS) bu çalışmaya
girmez; DSTKF (`configs/exclusions/symbols.json`) hiçbir listede yoktur.

## Ortak tasarım (dört hipotezin hepsi)

| Alan | Değer |
|---|---|
| Evren | `bist30_2026_holdout_v2`: 20 geliştirme + 10 holdout sembolü (`configs/splits/current.json` → v2) |
| Train/test sınırı | `train_test_cutoff: 2026-10-01` (endeks dönem değişikliğinin yürürlük günü; test dönemi henüz gerçekleşmemiş, ileriye dönük) |
| Zamanlama | Sinyal `t` kapanışından sonra hesaplanır (motor `t`'ye kırpılmış capture ile çalışır); giriş `t`'den sonraki ilk tradeable barın **açılışı** (`next_open`); çıkış girişten `h` tradeable bar sonraki kapanış |
| Vadeler | 1, 5, 10, 20 işlem günü; **birincil vade 5** (diğerleri ikincil/tanımlayıcı) |
| Maliyet | `--cost-bps 10`. Motor formülü `net = gross − 2 × cost_bps/10000` olduğu için bu **taraf başına** 10 bps = gidiş-dönüş 20 bps'dir (kaynaklı bir komisyon değeri değil, varsayım). `scripts/run_study.py --cost-bps` yardım metni ve sonuç JSON'u (`parameters.transaction_cost_basis`) artık "taraf başına (gidiş-dönüş = 2×)" der; formül değişmedi. Maliyet koşullu ve baseline olaylardan aynı miktarda düşüldüğü için birincil fark metriğini **değiştirmez**; yalnız mutlak net getirileri etkiler. |
| Düzeltme sınırı | `adjustment_boundary_policy: "drop"` (temettü/sermaye artırımı sınırını kesen pencere hem koşullu hem baseline'da düşer; sayılar raporlanır) |
| Birincil metrik | `Δ_5 = overall["5"].difference_mean` = ortalama(koşullu net getiri) − ortalama(baseline net getiri), baseline = aynı sembollerin kapsamdaki tüm sinyal günleri (koşullu kümenin üst kümesi) |
| Birincil test (tek) | Tek yönlü, **sinyal tarihine göre hareketli blok bootstrap** (E1): kapsamdaki sinyal tarihleri sıralanır, uzunluğu 5 olan ardışık tarih blokları iadeli çekilir (aynı tarihteki tüm sembol olayları birlikte), her örneklemde koşullu ve baseline ortalaması aynı yeniden örneklenmiş tarihlerden hesaplanır; `B = 9999`, seed `20261001` (spec'te `block_bootstrap`, sonuçta `overall[h].block_bootstrap_difference`). Beklenen yön negatifse `p = (1 + #{Δ*_b ≥ 0}) / (B + 1)`, pozitifse `p = (1 + #{Δ*_b ≤ 0}) / (B + 1)`. Uygulama ayrıntıları (motor, `fintr.research.engine.FORMULAS`): tarih ekseni = birincil vadede düşürülmemiş en az bir koşullu veya baseline olayı olan sinyal tarihleri (N); `ceil(N/5)` blok başlangıcı `0..N−5` arasından çekilir, birleşim N tarihe kesilir (dairesel değil); ortalamalar olay ağırlıklıdır; çekilen tarihlerde hiç koşullu olay yoksa `Δ*_b` tanımsızdır, sayılır ve p'de beklenen yönü **desteklemeyen** tarafa yazılır (muhafazakâr); yüzdelikler tanımlı `Δ*_b`'ler üzerinden en yakın sıra (`sorted[ceil(q·n) − 1]`). |
| Çoklu karşılaştırma | **Holm (1979)**, birincil aile = **yalnız H1 ve H2 (m = 2)**, aile bazında α = 0.05 (Efe kararı, [DECISIONS.md](DECISIONS.md) 2026-09-27). p'ler küçükten büyüğe `p(1), p(2)`; `p(1) ≤ 0.025` ise H(1) reddedilir ve `p(2) ≤ 0.05` ise H(2) de reddedilir; ilk başarısızlıkta durulur. **H3 ve H4 `secondary_descriptive`'dir: Holm'a girmez, m'yi değiştirmez**, E1 p'leri düzeltilmemiş ve yalnız tanımlayıcı raporlanır. Aile spec'lerin `preregistration` bloğunda (`family_role`, `primary_family_members`, `family_size`, `familywise_alpha`) tanımlıdır ve yükleyici tarafından doğrulanır; karar `scripts/evaluate_study_family.py` ile kaydedilmiş sonuçlardan verilir (eksik birincil üye hata verir). Train ve test aşamaları ayrı ayrı Holm'lanır; doğrulayıcı karar yalnız test aşamasınındır. |
| Asgari olay | Birincil vadede düşmeler sonrası ≥ 100 koşullu olay **ve** ≥ 30 farklı sinyal tarihi. Altında kalırsa sonuç "belirsiz — yetersiz olay"dır ve Holm'da `p = 1` alınır (aile boyutu değişmez). |
| SESOI | `δ = 0.0025` (5 günde 25 bps). Gidiş-dönüş maliyet varsayımının (20 bps) biraz üstü seçildi; bundan küçük bir fark bu tasarımda pratikte ayırt edilemez kabul edilir. Ön kayıt kararıdır, kaynaklı değer değildir. |
| i.i.d. bootstrap (ikincil) | Spec'lerde `confidence_level: 0.95`, `resamples: 2000`: m = 2 için Holm'un en katı adımındaki tek yönlü 0.025 düzeyine karşılık gelen iki yönlü aralık (m = 4 iken 0.975 idi; aile kararıyla birlikte güncellendi). i.i.d. olduğu için yalnız ikincil tanılamadır; motor yalnız havuzlanmış (`overall`) blokta E1 hesaplar, sembol bazında yalnız bu aralık vardır. |

### Sonuç sınıfları (test aşaması, her hipotez için)

Bu sınıflar yalnız birincil aile üyeleri (H1, H2) içindir; H3/H4
`descriptive_only` olarak raporlanır. `scripts/evaluate_study_family.py`
sınıfları aşağıdaki kurallarla üretir (`fintr.research.family.FORMULAS`).

- **Desteklendi:** Holm birincil testi beklenen yönde reddeder.
- **Yanlışlandı:** Holm reddetmez **ve** E1 bootstrap dağılımından tek yönlü
  %95 sınırı beklenen yönde SESOI'yi dışlar (beklenen yön negatifse
  `Δ*` dağılımının 5. yüzdeliği `> −δ`; pozitifse 95. yüzdeliği `< +δ`).
  Ters yönde anlamlı bir fark bu sınıfa girer.
- **Belirsiz:** Diğer her durum (ör. geniş aralık) veya asgari olay koşulu
  sağlanmadı. Belirsiz sonuç "etki yok" kanıtı değildir.

### Önceden kayıtlı ikincil analizler (doğrulayıcı değil)

Diğer vadeler (1/10/20), sembol bazında kırılım, motor v1 i.i.d. CI, hit
rate/medyan, piyasaya göre düzeltilmiş fark (E2), yalnız-holdout-sembolleri
havuzu (E5), endeks dönem değişikliği günleri ve yarım gün seansları hariç
duyarlılık, maliyet 0/25 bps duyarlılığı (farkı değiştirmez; yalnız mutlak
getiriler). H3 ve H4'ün E1 sonuçları (düzeltilmemiş p, Δ, yüzdelikler) ve
H3 − H1, H4 − H2 etki farkı yalnız tanımlayıcı olarak raporlanır.

## Hipotezler

| ID | Spec | Rol | Koşul (özet) | Birincil vade | Beklenen yön | Yanlışlama |
|---|---|---|---|---|---|---|
| [H1](H1.md) | `h1_close_premium_vs_vwap_proxy_v1.json` | birincil (Holm) | `-0.10 ≤ vwap_proxy_vs_close ≤ -0.01` (kapanış VWAP proxy'sinin ~%1+ üstünde) | 5 | Δ < 0 | Δ*'ın 5. yüzdeliği > −0.0025 |
| [H2](H2.md) | `h2_close_discount_vs_vwap_proxy_v1.json` | birincil (Holm) | `0.01 ≤ vwap_proxy_vs_close ≤ 0.10` (kapanış ~%1+ altında) | 5 | Δ > 0 | Δ*'ın 95. yüzdeliği < +0.0025 |
| [H3](H3.md) | `h3_concentrated_flow_close_premium_v1.json` | ikincil/tanımlayıcı | `top1_akd_net_share_of_volume ≥ 0.10` ve H1 koşulu | 5 | Δ < 0 | — (tanımlayıcı) |
| [H4](H4.md) | `h4_concentrated_flow_close_discount_v1.json` | ikincil/tanımlayıcı | `top1_akd_net_share_of_volume ≥ 0.10` ve H2 koşulu | 5 | Δ > 0 | — (tanımlayıcı) |

Neden yalnız dört ve neden takas yok: motorun kod-bağımsız özellikleri
yalnız `top1_akd_net_share_of_volume` ve `vwap_proxy_vs_close`'dur.
`flow_run_length` ve `custody_change` bir kurum/saklamacı **kod kümesi**
ister; kod kümesi seçimi ya veriye bakmayı ya da (ör. "yabancı saklamacı")
repoda doğrulanmamış bir sınıflandırmayı gerektirirdi. Kod uydurmamak ve
sınıflandırma varsaymamak için takas/akış-kalıcılığı fikirleri aşağıdaki
"yeni feature gerekiyor" listesine konuldu.

## Yeni feature gerekiyor (spec'lenmedi)

Hiçbiri motorda yok; hiçbiri için sonuç iddiası yoktur. Her biri kendi ADR/
test'iyle eklenip ayrı bir ön kayıt sürümünde kullanılmalıdır.

| ID | Formül (önerilen) | Gereken veri | Açacağı soru |
|---|---|---|---|
| N1 `signed_top1_akd_net_share` | `net_size[c*, t] / sum_total_size[t]`, `c* = argmax_c |net_size[c, t]|` (eşitlikte alfabetik ilk kod) | `daily_akd_net`, `daily_akd_market` (mevcut capture'lar) | Yoğunlaşmış akışın yönü, fiyattan (H3/H4'teki VWAP proxy'si) bağımsız olarak |
| N2 `topk_net_imbalance(k)` | `(Σ en büyük k pozitif net − Σ en büyük k |negatif net|) / sum_total_size[t]` | aynı | Tek kurum yerine ilk k alıcı/satıcı dengesizliği |
| N3 `vwap_proxy_within_bar_range` | `1` eğer `bar.low[t] ≤ vwap_proxy[t] ≤ bar.high[t]`, değilse `0`; eksikse `None` | AKD + aynı gün fiyat barı | H1–H4'e gün bazlı veri kalitesi koşulu (proxy varsayımı o gün tutarlı mı) |
| N4 `probable_price_rescale_flag` | `1` eğer `t` bir `ratio_regime` (`probable_retroactive_price_adjustment`) içinde | AKD proxy + fiyat hacmi | VWAP/kapanış birim uyumsuzluğunu bantla değil doğrudan dışlamak. Uyarı: rejim tespiti `t` sonrası günleri de okur; yalnız veri temizliği filtresi olarak, gerekçesiyle kullanılmalı |
| N5 `retail_pct_change(lag)` | `retail_pct[t] − retail_pct[t−lag]` (sunucu tarihi adımı); son `provisional_last_custody_days` tarihi `None` | `provider_metadata.retail_pct` | Sağlayıcı bireysel payı değişiminin ardından ileri getiri (takas T+2 ve sağlayıcı yöntemi doğrulanmamış) |
| N6 `custody_hhi_change(lag)` | `HHI(t) − HHI(t−lag)`, `HHI = Σ_c (lot[c]/Σ lot)^2`, tüm saklama kodları | `daily_custody_lots` | Kod seçmeden saklama yoğunlaşması değişimi; tek tip ölçek değişimine (bedelsiz) karşı değişmez |
| N7 `top1_buyer_persistence` | `t`'de biten, aynı kodun en büyük pozitif net alıcı olduğu ardışık sunucu tarihi sayısı | `daily_akd_net` | Kod seçmeden akış kalıcılığı (kurum kimliği/niyeti çıkarımı yok) |
| N8 `trailing_percentile(feature, W)` | `t`'deki değerin, aynı sembolün `(t−W, t]` sunucu tarihlerindeki değerleri arasındaki yüzdelik sırası | ilgili feature geçmişi (`≤ t`) | Sabit eşik yerine sembol-içi göreli eşik (hisseler arası ölçek farkı) |
| N9 `close_to_close_return` | `close[t] / close[t−1] − 1` (düzeltme sınırında `adjusted_close`) | fiyat geçmişi | VWAP-kapanış farkını günün toplam getirisinden ayırmak (klasik kısa vadeli geri dönüş kontrolü) |

## Motor yetenekleri (feature değil; dondurma/çalıştırma ön koşulları)

| ID | Gereken | Neden | Durum |
|---|---|---|---|
| E1 | Sinyal tarihine göre hareketli blok bootstrap, tek yönlü p ve tek yönlü sınırlar; farklı sinyal tarihi sayısı | Olaylar aynı gün hisseler arası ve 5 günlük örtüşen pencerelerle bağımlıdır; motor v1'in i.i.d. bootstrap'ı varyansı küçümser | **Uygulandı** (`engine.moving_block_bootstrap`; spec `block_bootstrap`; sonuç `block_bootstrap_difference`, `hypothesis_test`) |
| E2 | Piyasaya göre düzeltilmiş getiri: olay getirisi − aynı giriş/çıkış tarihlerinde kapsamdaki tüm sembollerin eşit ağırlıklı ortalaması | Olaylar zamanda kümelenirse ham fark piyasa zamanlamasını da ölçer | İkincil analiz için önerilir |
| E3 | Spec'te test penceresi bitişi (`test_end`) ve train fiyat bitişi zorlaması | Motor şimdi yalnız `>= cutoff` uygular; pencere operatör disipliniyle sınırlanıyor | **Uygulandı** (`engine.signal_dates_for_split`'in `window_end`'i; `run_study.py --test-plan` planın `test_end`'ini geçirir, sonuçta `test_window_end`) |
| E4 | Sembol başına birden çok dönem capture'ını birleştirme: tekrar eden tarihlerde sonraki capture öncelikli (son gün takas revizyonu), ek yerlerindeki boşlukların raporlanması, girdi hash'leri | AKD/takas istek başına ~3 ay; `run_study.py` sembol başına tek capture alıyor | **Uygulandı, dar yorumla** (`research.capture_merge`; aşağıya bkz.) |
| E5 | Sembol alt kümesi (yalnız holdout) için havuzlanmış istatistik | Motor test split'i geliştirme + holdout'u birlikte havuzlar | **Uygulandı** (`run_study.py --split test --test-scope secondary`, `symbol_scope="holdout_only"`; ikincil/sağlamlık, doğrulayıcı değil) |
| E6 | Olay bazında çıktı (sembol, sinyal, giriş/çıkış, net getiri, düşme nedeni) | Denetlenebilirlik ve E1'in bağımsız doğrulanması | Önerilir |

### E4 yorumu (belirsizlikler açıkça)

Tanım üç noktada belirsizdi; en dar yorum uygulandı:

- **"Sonraki capture"**: `captured_at_utc`'si daha geç olan capture. Aynı
  sıranın istek dönemi (`start`, `end`) sırasıyla da tutması şarttır;
  ikisi çelişirse (ör. eski bir dönemin sonradan geri doldurulması) veya
  iki capture aynı zamanı taşırsa birleştirme **reddedilir** — hangi
  okumanın kastedildiği tahmin edilmez.
- **Öncelik birimi**: tarih bazında bütün kayıt. Bir tarihi taşıyan en
  geç capture o tarihin tüm dated serilerini (`daily_akd_net`,
  `daily_akd_market`, `daily_custody_lots`, `metadata`) verir; kod bazında
  karıştırma yoktur. Dönem toplamları (`period_akd_net`, `custodian_diff`)
  birleştirilemez ve birleşik capture'da boştur (hiçbir motor feature'ı
  okumaz).
- **Boşluk raporu**: yalnız rapor, doldurma/interpolasyon yok. Her ek
  yerinde istek dönemleri arası takvim günü ve fiyat dosyasındaki
  tradeable günlerden hiçbir capture'da olmayanlar
  (`capture_merge[SYMBOL].seams[].missing_tradeable_dates`). Capture içi
  eksik günler raporlanmaz. Lag kullanan feature'lar (`custody_change`,
  `flow_run_length`) boşluk üzerinden bitişik sunucu tarihleri gibi
  okur; H1–H4 bunları kullanmaz.

Kullanım: aynı sembol için `--data` birden çok kez, aynı fiyat dosyasıyla
verilir; her girdi dosyasının SHA-256'sı `SYMBOL:period_capture:<i>`
anahtarıyla, çakışan tarihler ve hangi serinin revize edildiği
`capture_merge` bloğunda kaydedilir.

## Ortak karıştırıcılar ve azaltma

- **Kurumsal işlem / lot ölçeği değişimi.** Yahoo split/bedelsizde `close`'u
  geriye dönük ölçekler, AKD `total_cost`'u ölçeklemez (`docs/features.md`,
  `ratio_regime`). Olaydan önceki günlerde `vwap_proxy_vs_close ≈ k − 1`
  (k ≥ 1.1 için ≥ 0.10) çıkar. Azaltma: tüm koşullarda `|vwap_proxy_vs_close|
  ≤ 0.10` bandı. Bildiğim kadarıyla BIST pay piyasasında günlük fiyat
  marjı ±%10'dur (doğrulanmadı); bu durumda meşru bir günde bile |fark|
  kuramsal olarak ~0.22'yi aşamaz ve gerçekte 0.10'u nadiren aşması beklenir.
  Bant, küçük oranlı bedelsizlerde (k < 1.1) uyumsuzluğu dışlayamaz → N4.
  Temettü/sermaye artırımı pencereleri `drop` ile düşer.
  `top1_akd_net_share_of_volume` aynı günün AKD'si içinde bir oran olduğu
  için ölçek değişiminden etkilenmez.
- **Tatil yer tutucuları.** Motor `placeholder` barları tradeable saymaz ve
  fiyat için kullanmaz. Yarım gün seansları (ör. bayram arifeleri) özel
  işlenmez; kapanış/VWAP dinamiği farklı olabilir → ikincil duyarlılık.
- **Son gün takas revizyonu, T+2.** H1–H4 takas okumaz; takas ölçek sıçraması
  denetimi bu yüzden tetiklenmez. N5/N6 kullanılırsa: takas bakiyesi `t` için
  en fazla `t−2` işlemlerini yansıtır ve her capture'ın son takas günü
  provizyoneldir (E4 birleştirmede sonraki capture öncelikli). Ayrıca motor
  varsayılanı `custody_scale_jump_ratio = 3` %50/%100 bedelsizleri
  yakalamaz; takas feature'larıyla daha düşük bir oran önceden kaydedilmeli.
- **2026-10-01 endeks değişikliği.** DSTKF çıkar (çalışmada zaten yok),
  TRMET girer (geliştirme sembolü). Endeks giriş/çıkış etkileri
  literatürde belgelidir (Harris & Gurel 1986, JF; Shleifer 1986, JF —
  S&P 500 için). Yürürlükten önceki son seansın kapanışında endeks fonu
  yeniden dengelemesi kapanış–VWAP farkını büyütebilir. `2026-09-30` sinyali
  train fiyat dosyası o gün bittiği için zaten düşer. Diğer dönemsel
  değişiklik günleri (üç ayda bir) repoda kayıtlı değil → duyarlılık
  analizi için tarihleri Efe birincil kaynaktan girmeli. TRMET'in endekse
  alınması son dönem büyüme/likidite artışını yansıtabilir; train baseline'ı
  bu sembolde yukarı yanlı olabilir.
- **Mevcut liste hayatta kalma yanlılığı.** Evren bugünkü BIST 30 anlık
  görüntüsüdür (ikincil web kaynakları); train dönemi (2025-10 → 2026-09)
  için geriye dönük üyelik doğrulanmadı. Koşullu−baseline farkı aynı
  semboller içinde alındığı için seviye yanlılığının çoğu farklanır, ama
  koşul sıklığı yükseliş dönemleriyle ilişkiliyse kalıntı yanlılık kalır.
  Test dönemi ileriye dönük olduğu için bu yanlılıktan büyük ölçüde
  arınmıştır.
- **Veri kapsamı.** Fiyat 2023'ten itibaren; AKD/takas istek başına ~3 ay
  (E4 gerekir). Sağlayıcının AKD geçmişini ne kadar geriye verdiği
  bilinmiyor; train penceresi hedefi `2025-10-01`, fiilen erişilebilen
  başlangıç train çalıştırmasından **önce** girdi manifestine yazılır ve
  alternatif pencere denenmez.
- **Kaynaklar resmî değil.** Kapanış/açılış Yahoo'dan (resmî olmayan);
  `vwap_proxy` ve `sum_total_size` `derived_proxy_not_official_ohlc`
  varsayımına dayanır. Proxy kontrollerinin (`vwap_within_range`,
  `net_within_total` vb.) geçme oranları train öncesi getiriye bakmadan
  raporlanır; ciddi başarısızlık Efe'ye bildirilir, spec getiriye bakılarak
  değiştirilmez.

## Güç / fizibilite (varsayıma dayalı)

Varsayımlar (veriden değil, yalnız ölçek fikri için): 5 günlük getirinin
standart sapması `σ_5 ≈ %5`; tek yönlü düzey `0.025` (m = 2 Holm'un en
katı adımı, z = 1.96; m = 4 iken 0.0125, z = 2.24 idi), güç 0.80
(z = 0.84). En küçük saptanabilir etki `MDE ≈ 2.80 × σ_5 / √n_eff`:

| n_eff | 30 | 50 | 100 | 200 | 400 |
|---|---|---|---|---|---|
| MDE (5 gün) | ~%2.6 | ~%2.0 | ~%1.4 | ~%1.0 | ~%0.7 |

`n_eff` ham olay sayısından belirgin küçüktür: aynı gün birçok sembolde
tetiklenen olaylar piyasa faktörü üzerinden korelelidir ve ardışık günlerin
5 günlük pencereleri örtüşür. Kaba olarak ham olayların 1/3 ile 1/10'u
beklenebilir (varsayım).

- **~60 işlem günü × 20 hisse = ~1.200 sembol-gün.** Koşul günlerin %10'unda
  tutarsa ~120 olay, `n_eff` ~15–40 → MDE ~%2.2–3.6. Yalnız çok büyük etkiler
  görülebilir; H3/H4 (daha seyrek) büyük olasılıkla 100 olay eşiğinin altında
  kalır. **Yeterli değil.**
- **~250 işlem günü × 20 hisse = ~5.000 sembol-gün.** %10 sıklıkta ~500 olay,
  `n_eff` ~50–170 → MDE ~%1.1–2.0. H1/H2 için sınırda; H3/H4 koşul
  sıklığına bağlı (bilinmiyor).
- **Test: ~125 işlem günü × 30 hisse = ~3.750 sembol-gün**; benzer büyüklük.

Sonuç: BIST 30 için beklenen etki büyüklüğüne dair güvenilir, kaynaklı bir
önselim yok; bu tasarım büyük olasılıkla yalnız yüzde birler mertebesindeki
etkileri saptayabilir ve "belirsiz" sonuç olası bir çıktıdır. Olay sayısı eşiklerinin
koşul sıklığına göre sonradan gevşetilmemesi bu yüzden önemlidir.

## Efe'nin kararları (dondurmadan önce)

2026-09-27 yanıtları [DECISIONS.md](DECISIONS.md)'dedir (1: eşikler sabit;
2: pencere onaylandı; 3: E1 ve E4 dondurmadan önce — uygulandı; 4: birincil
aile yalnız H1–H2, H3/H4 ikincil/tanımlayıcı). Aşağıdaki liste kayıt için
korunmuştur.

1. Dört hipotez, eşikler (`0.01`, `0.10` bant, `top1 ≥ 0.10`) ve birincil
   vade 5 onaylanıyor mu? Alternatif: eşikleri **yalnız feature dağılımına**
   (getiri okumadan) bakarak geliştirme train verisinde yüzdelik tabanlı
   belirlemek — bu da dondurmadan önce yapılmalı ve belgelenmelidir.
2. Test penceresi `2026-10-01..2027-03-31` uygun mu (daha uzun pencere gücü
   artırır, sonucu geciktirir)?
3. E1 ve E4 train'den önce uygulansın mı (öneri: evet; aksi halde doğrulayıcı
   sonuç yok)?
4. Holm ailesi yalnız H1–H4 mü (N-listesinden eklenecek hipotezler ayrı aile
   ve ayrı ön kayıt sürümü olur)?

## Doğrulama

```bash
pip install websockets
PYTHONPATH=src python -m unittest discover -s tests -p "test_prereg_specs.py" -v
PYTHONPATH=src python -m unittest discover -s tests -p "test_study_prereqs.py" -v
```

Train sonrası aile kararı (kaydedilmiş dört sonuçtan; H3/H4 Holm dışı
listelenir):

```bash
PYTHONPATH=src python scripts/evaluate_study_family.py \
  --result data/interim/studies/prereg_bist30_h1_close_premium_vs_vwap_proxy_v1_train.json \
  --result data/interim/studies/prereg_bist30_h2_close_discount_vs_vwap_proxy_v1_train.json \
  --result data/interim/studies/prereg_bist30_h3_concentrated_flow_close_premium_v1_train.json \
  --result data/interim/studies/prereg_bist30_h4_concentrated_flow_close_discount_v1_train.json
```

`tests/test_prereg_specs.py` her spec'i `fintr.research.study_spec.
load_study_spec` ile yükler; `frozen: false`, açıklama öneki, sembollerin
dondurulmuş split'le eşitliği (split dosyası SHA-256'sı dahil), DSTKF'nin
yokluğu, ortak tasarımın aile içinde aynılığı, `preregistration` bloğunun
tutarlılığı, birincil ailenin H1/H2 (m = 2) olduğunu, H3/H4'ün
`secondary_descriptive` olduğunu, E1 parametrelerini ve koşulların yalnız
mevcut motor feature'larını kullandığını denetler. `preregistration`
bloğunun bir alt kümesi (hipotez kimliği, aile rolü/üyeleri/boyutu, α, yön,
birincil vade, asgari olay/tarih, SESOI) yükleyici tarafından okunur ve
doğrulanır; bloğun tamamı `spec_sha256`'ya dahildir. `preregistration`
taşıyan bir spec `block_bootstrap` olmadan yüklenmez.

## Holdout testi çalıştırma

Nisan 2027'de, pencere (`2026-10-01..2027-03-31`) bittikten ve fiyat/AKD
verisi toplandıktan sonra, tek seferlik test şu sırayla çalıştırılır.
`scripts/run_holdout_test.py` **varsayılan olarak `--dry-run`'dır**; hiçbir
dosya yazmaz, yalnız aşağıdaki altı denetimi ✓/✗ olarak listeler. Gerçek
çalıştırma için açıkça `--execute` gerekir.

1. **Toplama.** Pencereyi kapsayan (`start >= 2026-10-01`) dönem kayıtları
   `data/interim/period_analysis/<SYM>/` altına, 30 sembolün tamamı için
   (çok hisseli koleksiyon, `live_collector_smoke.py --symbols ...
   --allow-large-range --symbol-pause-seconds 30`). Pencere öncesi kayıt
   çalıştırıcı tarafından kullanılmaz (bkz. aşağıdaki "Holdout sembollerinin
   2026-10-01 öncesi verisi..." kuralı, artık 30 sembolün hepsi için
   geçerlidir).
2. **Fiyat güncelleme.** `data/interim/price_history/<SYM>/` fiyat geçmişi,
   pencere sonundan (`2027-03-31`) sonra en az 25 tradeable bar içerecek
   şekilde güncellenir (`scripts/fetch_price_history_batch.py`).
3. **Dry-run.**

   ```bash
   PYTHONPATH=src python scripts/run_holdout_test.py \
     --test-plan configs/studies/prereg/test_plan_v1.json
   ```

   Altı denetim: (1) plan ve dört spec hash'i doğru; (2) git çalışma ağacı
   temiz; (3) her sembol için pencere içinde başlayan (`start >=
   2026-10-01`) en az bir dönem kaydı var (varsa E4 ile birleştirilir); (4)
   fiyat geçmişinde `2027-03-31` sonrası en az 25 tradeable bar var; (5) AKD
   sunucu tarihleri pencere içindeki tradeable fiyat günlerini kapsıyor; (6)
   kilit (`data/interim/studies/test/LOCK`) veya önceki test çıktısı
   (`manifest.json`) yok. Hepsi ✓ olana kadar tekrar toplama/güncelleme
   yapılır; dry-run hiçbir zaman dosya yazmaz.
4. **Execute.**

   ```bash
   PYTHONPATH=src python scripts/run_holdout_test.py \
     --test-plan configs/studies/prereg/test_plan_v1.json --execute
   ```

   Önce tüm dry-run denetimleri yeniden çalıştırılır; biri bile ✗ ise
   hiçbir şey çalışmaz. Geçerse ilk iş `LOCK` dosyasının atomik (`O_EXCL`)
   oluşturulmasıdır -- kilit zaten varsa (yarışma durumu dahil) çalıştırma
   reddedilir, manifest tamamlanmış olsa bile. Sonra dört spec × iki
   `--test-scope` (`primary`: 30 sembolün tamamı, `symbol_scope="declared"`;
   `secondary`: yalnız 10 holdout sembolü, `symbol_scope="holdout_only"`,
   E5) `scripts/run_study.py --split test --save` ile çalıştırılır, ardından
   `scripts/evaluate_study_family.py` yalnız primary H1+H2 sonuçları
   üzerinde (Holm, m=2). Son olarak `data/interim/studies/test/manifest.json`
   yazılır: tüm girdi/çıktı dosya hash'leri, kod commit'i, plan hash'i,
   zaman damgası, `completed` bayrağı. Yarım kalmış bir çalıştırma (kilit
   var ama manifest yok veya `completed: false`) bir sonraki çalıştırmayı
   otomatik tekrar denemez; Efe'nin kararı gerekir. Stdout'a yalnız dosya
   yolları ve ✓/✗ durumları yazılır -- getiri, p değeri veya karar metni
   yalnız kaydedilen JSON dosyalarındadır, hiçbir zaman terminale
   basılmaz.

**Karara bağlandı (2026-09-28, DECISIONS.md "Holdout fiyat geçmişi kararı"): tam geçmiş kullanılır.** Önceki açık soru: `fintr.features.price_join.
join_price_history`'nin düzeltme sınırı ve ratio-regime tespiti, sembolün
**kaydedilmiş fiyat geçmişinin tamamını** okur -- bu, motorun train
çalıştırmalarında zaten kullandığı, belgelenmiş davranıştır (bkz.
`docs/study_engine.md` "Custody scale jumps" / "Ratio-regime" bölümleri),
`run_holdout_test.py`'nin değiştirdiği bir şey değildir. Holdout sembolünün
fiyat dosyasının test penceresinden önceki kısmı bu tespit için kullanılmaya
devam edecek mi (train'in yaptığı gibi) yoksa yalnız pencereyle mi
sınırlanmalı -- Efe henüz karar vermedi; araç mevcut (tam geçmiş) davranışı
korur ve bu soruyu burada işaretler, kendi başına seçim yapmaz.

## Kaynaklar

Gerçek, bildiğim yayınlar (künyeler hafızadan; sayfa/sayı ayrıntıları
bağımsız olarak doğrulanmadı):

- Holm, S. (1979). A simple sequentially rejective multiple test procedure.
  *Scandinavian Journal of Statistics*, 6(2).
- Künsch, H. R. (1989). The jackknife and the bootstrap for general
  stationary observations. *Annals of Statistics*, 17(3).
- Harvey, C. R., Liu, Y., & Zhu, H. (2016). … and the cross-section of
  expected returns. *Review of Financial Studies*, 29(1).
- Harris, L., & Gurel, E. (1986). Price and volume effects associated with
  changes in the S&P 500 list. *Journal of Finance*, 41(4).
- Shleifer, A. (1986). Do demand curves for stocks slope down? *Journal of
  Finance*, 41(3).
- Hipoteze özgü kaynaklar ilgili `H*.md` dosyalarındadır.
- **Belirsiz:** BIST'e özgü, aracı kurum dağılımı (AKD) verisiyle ileri
  getiri ilişkisini inceleyen hakemli bir çalışmayı doğrulanmış künyeyle
  gösteremiyorum; bu nedenle hiçbir hipotez böyle bir kaynağa dayandırılmadı.
