# Ön kayıt kararları

## 2026-09-27 — Efe'nin kararları (yönetici önerisi üzerine)

Kaynak: Efe'nin Claude Code yönetici oturumundaki onayı ("Tamam hipotezler böyle olsun").
Bu kararlar spec'leri henüz dondurmaz; dondurma, aşağıdaki motor ön koşulları
karşılandıktan ve geliştirme verisi toplandıktan sonra, getirilere bakılmadan yapılır.

1. **Birincil aile:** yalnız H1 ve H2 (m = 2, Holm, aile α = 0.05).
   H3 ve H4 birincil aileden çıkarıldı; önceden kayıtlı **ikincil/tanımlayıcı**
   analiz olarak raporlanır, doğrulayıcı karar üretmez.
   Gerekçe: README'deki güç notuna göre H3/H4 büyük olasılıkla asgari olay
   eşiğinin altında kalır; aileyi küçültmek H1/H2'nin gücünü artırır.
2. **Eşikler sabit:** `0.01` sapma eşiği ve `±0.10` bant spec'lerdeki gibi kalır;
   veriye bakılarak yüzdelik tabanlı eşik seçilmez.
3. **Birincil vade 5 işlem günü; test penceresi 2026-10-01..2027-03-31**
   (ileriye dönük, tek seferlik holdout testi).
4. **Motor ön koşulları dondurmadan önce:** README'deki E1 (sinyal tarihine göre
   hareketli blok bootstrap) ve E4 yetenekleri uygulanır; `--cost-bps` yardım
   metni ile formül arasındaki taraf başına / gidiş-dönüş tutarsızlığı düzeltilir.
5. **Geliştirme verisi:** ~1 yıllık AKD/takas verisi toplanır (3 aylık veri güç
   açısından yetersiz). Holdout sembollerinin verisi de toplanır ama geliştirmede
   kullanılmaz; DSTKF dışlanmıştır.

## 2026-09-27 — Dondurma öncesi veri kalitesi kuralı (getirilere bakılmadan)

Kaynak: yönetici oturumu; yalnız AKD-türevi lot / Yahoo hacim oranına bakıldı,
hiçbir getiri okunmadı. 20 geliştirme hissesinin 19'unda oran ≈ 1 (tek sapma
kesinleşmemiş son gün). BIMAS'ta 2025-09-26..2026-05-13 arası 159 günde oran
tam 0,5: Yahoo'nun geriye dönük ölçek düzeltmesi (1'e 2) ile AKD'nin düzeltilmemiş
lotları arasında ölçek uyumsuzluğu.

Kural (spec'ler dondurulmadan önce motora eklenir): `price_join` oran rejimi
(`probable_retroactive_price_adjustment`, tam sayılı k ≥ 2) içindeki sinyal
günleri hem koşullu hem baseline kümesinden düşülür; düşülen gün sayısı sembol
bazında raporlanır. Getiriler Yahoo'nun kendi içinde tutarlı serisinden
hesaplandığı için çıkış penceresi rejim içinde kalan olaylar ayrıca düşülmez.

## 2026-09-28 — Test kapsamı kararı (Efe onayı, yönetici önerisi; test verisi toplanmadan)

1. **Birincil test:** Dondurulmuş spec'lerin bildirdiği 30 sembolün tamamı (20 geliştirme + 10 holdout); sinyal penceresi 2026-10-01..2027-03-31 (zaman dışı test). Bu, README 6. maddedeki motor tanımıdır (`symbol_scope="declared"`). Holm (m=2, H1+H2) yalnız bu birincil sonuçlara uygulanır.
2. **İkincil (E5, sağlamlık, doğrulayıcı değil):** Yalnız 10 holdout sembolü (`symbol_scope="holdout_only"`); aynı pencere ve parametreler.
3. **Pencere bitişi (E3) zorlanır:** 2027-03-31 sonrası sinyal tarihi dahil edilmez. Her sembolün fiyat verisi pencere sonundan sonra en az 25 tradeable bar içermelidir.
4. **Tek sefer:** Test bir kez çalışır. Çıktı veya kilit varsa tekrar çalışmaz. Sonuç ne olursa olsun raporlanır.

Gerekçe: 2026-10-01 sonrası veri hiçbir sembol için görülmedi ve spec'ler train getirileri okunmadan donduruldu. 30 sembol daha fazla olay (güç) sağlar. Holdout-only analizi, hiç görülmemiş sembollerde ek bir kontrol sağlar.

Test planı config'i: `configs/studies/prereg/test_plan_v1.json` (şema `fintr_test_plan_v1`), SHA-256 `b5070b39ed17c5c432cb2567fdb1917d52c1a4e4f552c4ae114a747f11d9182a`.

## 2026-09-28 — Holdout fiyat geçmişi kararı (Efe onayı, yönetici önerisi; test verisi toplanmadan)

Holdout testinde her sembolün kaydedilmiş fiyat geçmişinin **tamamı** (test_start öncesi
dahil) kullanılmaya devam eder; dosya pencereye kırpılmaz. Kullanım yalnız veri kalitesi
tespitiyle sınırlıdır: `fintr.features.price_join.join_price_history` düzeltme sınırı ve
ratio-regime tespiti (train'deki mevcut davranış). Sinyal, koşul değerlendirmesi ve getiri
hesabı pencere öncesi fiyatı kullanmaz; pencere öncesi AKD/takas kayıtları yürütücü
tarafından reddedilmeye devam eder.

Bu, README'deki "holdout sembollerinin 2026-10-01 öncesi verisi hiçbir aşamada
kullanılmaz" kuralına yalnız bu veri kalitesi kullanımı için açık bir istisnadır.

Gerekçe: rejim/düzeltme tespiti uzun seriye dayanır; kırpmak pencere başında başlayan
bir ölçek değişikliğini kaçırma riski doğurur. Kural, AKD/takas analizine yönelik
sızıntıyı önlemek için konmuştur ve bu kullanım sonuç değişkenine dokunmaz.

## 2026-10-02 — Bildirimde beklenen yön etiketi (Efe kararı)

Efe 2026-10-02'de sohbette açıkça ("evet ekle") karar verdi: deterministik Telegram
bildirim botu (`fintr.notify`), her `condition_met: true` gözlemi, ilgili dondurulmuş
kuralın varsayılan hipotez yönüyle etiketleyebilir.

**İzin verilen.** Her gözlem satırına, ilgili dondurulmuş spec'in
`preregistration.predicted_direction` alanından türetilen etiket eklenebilir:
`positive` → "beklenen yön: yukarı (al yönlü)", `negative` → "beklenen yön: aşağı
(sat yönlü)". Yön, tarayıcının mevcut hash doğrulamalı kural yükleyicisiyle
(`fintr.scanner.rules.load_scanner_rules`) okunur; kural çözülemez, hash
doğrulaması başarısız olur ya da alan tam olarak `positive`/`negative` değilse satır
etiketsiz kalır ve "yön bilinmiyor" işareti taşır (tahmin edilmez). Etiket içeren her
mesaj bir kez şu uyarıyı taşır: "Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir
(holdout testi 2027 Nisan); yatırım tavsiyesi değildir." Etiket yalnız Efe'nin kişisel
kullanımı içindir; getiri, puan, sıralama, hedef fiyat, stop seviyesi veya pozisyon
büyüklüğü eklenmez. LLM bileşenleri bu karara dahil değildir. `--no-direction-label`
bayrağı önceki sözleşmeyi (etiketsiz metin) geri getirir; varsayılan etiketli.

**Değişmeyen.** Spec hash'leri (`docs/prereg/FREEZE.md`), eşikler, tarayıcı kuralları
ve çıktı şeması, holdout planı (`test_plan_v1.json`) ve K4 (2027-03-31'e kadar ara
performans değerlendirmesi yok) aynen geçerlidir. Etiket gözlemin kendisine hiçbir bilgi
eklemez: yön zaten dondurulmuş spec'te kayıtlıydı; bu karar yalnız ifadeyi (mesaj
metnini) değiştirir.

**Kanıt durumu.** Yönetici oturumu train sonuçlarını okumamıştır ve bir avantaj (edge)
iddiasında bulunmaz. Efe, bunun kişisel kullanım olduğunu ve etiketin tam olarak
güvenilir olmadığını bildiğini belirtti. Önceki plan, yön ifadesini "kanıt sonrası
sinyal" olarak holdout sonucundan sonraya bırakmaktı; bu karar ifadeyi kanıt olmadan öne
çeker. Bu nedenle etiket doğrulanmamış hipotez olarak sunulur ve holdout sonucu
beklenen yönü doğrulamazsa etiket kaldırılmalıdır.

**Kapsam dışı.** Bu karar tarayıcı çıktı şemasını veya LLM bileşenlerini
kapsamaz; AGENTS.md "Güvenlik sınırları" bölümündeki istisna yalnız
`fintr.notify` içindir.

## 2026-10-02 — Günlük analiz notu: Claude Opus 5.5 kanıt değerlendirmesi (Efe kararı)

Efe 2026-10-02'de sohbette doğrudan üç karar verdi. ("Nihai kararı da versin" dedi;
yönetici kişisel al/sat kararı yerine kanıt değerlendirmesini önerdi ve karar bu
biçimde kayda geçti.)

1. **Günlük analiz notu adımı.** Günlük akışa Claude Opus 5.5 ile bir analiz notu adımı
   eklenir. Not, `condition_met` olan her gözlem için kanıta dayalı **nihai
   değerlendirme** içerir: dondurulmuş kuralın yönünü veri `destekliyor` /
   `zayıflatıyor` / `belirsiz`. Bu bir hipotez değerlendirmesidir. Model "al/sat",
   hedef fiyat, stop, pozisyon büyüklüğü, getiri tahmini veya 1–10 tarzı keyfi puan
   üretmez. İşlem kararı Efe'dedir.
2. **Ham veri girdisi.** Modele özet değil **ham veri** gider: yalnız `condition_met`
   gözlemi olan hisselerin birleşik kayan pencere verisi (kurum bazlı AKD/takas
   satırları, kurum adlarıyla, `as_of` ve öncesi), gözlem dosyası, hesaplanmış
   özellikler, veri durumu sayımları ve kural spec metinleri. **Risk notu:** Mini App
   lisans beyanı (`user_asserted_2026-09-28_not_independently_verified`) veri çekimi
   içindi; üçüncü tarafa (Anthropic) gönderimi kapsadığı doğrulanmadı — bu Efe'nin
   kararıdır.
3. **Çağrı yolu.** Model çağrısı Efe'nin aboneliğiyle, **Claude Code CLI başsız mod**
   (`claude -p`) ile yapılır; ayrı API anahtarı yoktur (kod, çocuk sürecin ortamından
   `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` değişkenlerini çıkarır).

**Kodla zorlanan sınırlar** (`fintr.llm_note`, `scripts/llm_note.py`):

- Girdi paketi `as_of` sonrası hiçbir tarihli veri içermez (`truncate_period_capture`
  + tarih süzgeci); fiyat serisi, getiri veya gözlem sonrası fiyat paketten çıkarılır
  ve hesaplanmaz (K4 aynen geçerli). Holdout ve dışlanan hisseler (DSTKF) mevcut
  `load_active_guards` ile reddedilir; model çağrılmaz.
- Çıktı yapılandırılmış JSON'dur (şema `configs/llm_note/output_schema_v1.json`);
  izinli değerlendirme değerleri `destekliyor`/`zayıflatıyor`/`belirsiz`.
- Hisse başına bir paket ve bir model çağrısı; paket hiçbir zaman kırpılmaz (sınırı
  aşan hisse "alınamadı (packet_too_large)", hisse sayısı sınırını aşan
  "alınamadı (symbol_limit)"; her hisse kendi sonucunu alır).
- Doğrulayıcı fail-closed'dur: şema, izinli değer, kapsam (her gözlem için tam bir
  değerlendirme), kanıt yolları (pakette çözülmek ve dar olmak zorunda), yasak ifade
  listesi (al/almalı, sat/satmalı, hedef, stop, pozisyon, kesin, kâr/zarar, getiri,
  tahmin, öneri, tavsiye, puan/skor, fırsat, garanti, manipülasyon, niyet,
  yükselecek/düşecek/bekleniyor…), yazıyla sayı ve gerekçedeki her rakamın/tarihin o
  gerekçenin kendi kanıt yolları altında bulunması (işaret eşleşir; `%` için `p` aynen
  veya `p/100` yalnız `|v| <= 1`). Biri başarısızsa not reddedilir ("analiz notu
  doğrulamadan geçemedi (<kategori>)"); model reddi/limit/zaman aşımı "analiz notu
  alınamadı (<kategori>)" olur. Not başarısız olursa deterministik bildirim yine
  gönderilir.
- Prompt, şema ve config Git'tedir; sha256'ları ile model kimliği, CLI sürümü, komut
  satırı, ham yanıt, çağrı süresi/kullanımı ve doğrulama sonucu
  `data/interim/llm_notes/<as_of>/<SYM>/attempt-NNN/` altına no-overwrite kaydedilir.
  LLM çıktısı deterministik değildir.

**Değişmeyen.** Spec hash'leri, eşikler, tarayıcı kuralları ve çıktı şeması, holdout
planı ve K4 aynen geçerlidir; deterministik bildirim metni (`--llm-note-dir` verilmediğinde)
bayt düzeyinde aynıdır; adım `run_daily_pipeline.py --llm-note` ile açılır, varsayılan
kapalıdır. Not bir avantaj (edge) iddiası değildir: kuralın yönü doğrulanmamış bir
hipotezdir (holdout testi 2027 Nisan) ve değerlendirme yalnız ham verinin o hipotezle
ilişkisini anlatır. Holdout sonucu yönü doğrulamazsa bu adım kapatılmalıdır.

## 2026-10-03 — Telegram "sinyal chati": kısa bildirim düzeni v3 (Efe kararı)

Efe 2026-10-03'te Telegram'ı "sinyal chati" olarak kullanmaya karar verdi: günde tek
mesaj, hisse başına tek satır; uzun açıklamalar ve "Ne demek" metni bu modda yoktur.
Karar, 2026-10-02 tarihli iki istisnanın (yön etiketi; analiz notu) ifadesini günceller;
kural, eşik, spec, holdout planı ve K4 aynen geçerlidir.

**İzin verilen.**

- Düzen v3'te (`fintr.notify.layout_v3`, varsayılan) yön etiketi kısa biçimde gösterilebilir:
  `positive` → `↑ al yönlü`, `negative` → `↓ sat yönlü`. Yön yine yalnız dondurulmuş
  spec'in `predicted_direction` alanından (hash doğrulamalı yükleyici) gelir; çözülemezse
  ya da `--no-direction-label` verilmişse yön kısmı yazılmaz (tahmin yok). Düzen v1/v2 aynen
  kalır (uzun etiket).
- Doğrulanmamış hipotez uyarısı her mesajın başlığındadır: `📊 <GG.AA> sinyaller
  (doğrulanmamış hipotez, tavsiye değil)`. Gözlem yoksa tek satır: `📊 <GG.AA> sinyal yok`.
- Aynı satırda, o hisse ve kural için **kabul edilmiş (`accepted`)** analiz notunun kanıt
  değerlendirmesi kelimesi (`destekliyor`/`zayıflatıyor`/`belirsiz`) gösterilebilir
  (`Opus: <kelime>`). Not yoksa, reddedilmişse, başarısızsa ya da bütünlük denetimini
  geçemiyorsa `—` yazılır. Kelime, doğrulanmış notun alanının aynen aktarımıdır; birleştirilen
  kurallarda kelimeler farklıysa kural başına gösterilir, kod hiçbir kelime üretmez ya da
  birleştirmez.

**Değişmeyen / yasak.** LLM al/sat, hedef, puan ya da karar üretmez; bu yasak aynen kalır
(analiz notunun yasak ifade listesi ve doğrulayıcısı gevşetilmedi). Satırda getiri, puan,
hedef fiyat, stop seviyesi, pozisyon büyüklüğü, kural açıklaması ya da "Ne demek" metni
yoktur. Bildirim nota bağlı değildir: `llm_note.py` hata verir ya da zaman aşımına uğrarsa tek
v3 mesajı yine gönderilir, değerlendirmeler `—` olur. v3'te ayrı not mesajı gönderilmez.
Receipt ve devam mekanizması düzen v2 ile aynıdır (`receipt*.json`); aynı gün yeniden
çalıştırmada yeniden gönderim olmaz. Veri satırı yalnız pencerede kesinleşmemiş gün,
boşluk ya da revize varsa eklenir (kayıtlı `composition` sayımlarından; yeni rakam üretilmez).

**Kanıt durumu.** Etiket ve kelime avantaj (edge) iddiası değildir; yön doğrulanmamış
hipotezdir (holdout testi 2027 Nisan) ve K4 (2027-03-31'e kadar ara performans
değerlendirmesi yok) geçerlidir. Holdout sonucu yönü doğrulamazsa etiket ve `Opus:` kelimesi
kaldırılmalıdır. Ham verinin Anthropic'e gönderimi hakkındaki 2026-10-02 notu (lisans kapsamı
doğrulanmadı) değişmedi.

**Analiz notu istemi (prompt v3).** TRALT notu iki kez `forbidden_phrase: getiri` ile
reddedildi: model hipotezi anlatırken "getiri" sözcüğünü kullandı (kural metni bu sözcüğü
içeriyor). `configs/llm_note/prompt_v3.md` = v2 + açık kural: "getiri", "kazanç", "kâr"
kullanılmaz; hipotez "hipotez, sonraki 5 işlem gününde fiyat hareketinin ... olacağını
varsayar" kalıbıyla anlatılır. `config_v1.json` `prompt_file` v3'e işaret eder; v2 dosyası
değişmedi; yasak liste ve doğrulayıcı gevşetilmedi.

## 2026-10-14 — Analiz notu holdout dönemi çalışmalarına girdi olamaz (Efe kararı)

Efe onayı 2026-10-14 (yönetici oturumu, "evet, 1 2 4'ü başlat"):

- Analiz notunun kanıt değerlendirmesi kelimesi (`destekliyor`/`zayıflatıyor`/`belirsiz`) ve
  metni **hiçbir** istatistiksel çalışmaya, holdout testine, model seçimine, kural/eşik
  seçimine ya da holdout dönemi verisiyle yapılan istem (prompt) optimizasyonuna girdi
  olmaz. 2026-10-01'den itibaren günlük veri holdout penceresindedir (2026-10-01..2027-03-31).
  Gerekçe: LLM ileriye bakış/ezberleme riski (Gao, Jiang, Yan, arXiv 2512.23847; yalnız özet
  okundu) ve AGENTS.md eval-verisi kuralı; bkz.
  `docs/research/signal_bot_best_practices_2026-10-13.md` F2.
- Geçmiş bir `as_of` için sonradan üretilen notlar (geç yeniden deneme/backfill) işaretlenir:
  kayıtta `generated_late` (Europe/Istanbul üretim günü `as_of`'tan sonraki ilk hafta içi
  günden sonraysa `true`; cuma → pazartesi geç değil; tatiller yok sayılır, repoda takvim yok) ve `generated_on_local`.
- Her not kaydı model kimliğini ve üretim zamanını taşır (`model_requested`, `model_reported`,
  `started_at_utc`; `record.json`).
- Her istem/doğrulayıcı sürüm değişikliği, değerlendirilmeden önce `docs/prereg/TRIAL_LOG.md`'ye
  yazılır.

Kural, eşik veya spec değiştirmez. K4 ve holdout testi aynen geçerlidir.

## 2026-10-16 — K4 yalnız geliştirme hisseleri için kaldırıldı: yürüyen isabet izleme (Efe kararı)

Efe (proje sahibi) 2026-10-16'da karar verdi:

- **K4** ("2027-03-31'e kadar H1-H4 için ara performans değerlendirmesi yok") **yalnız 20
  GELİŞTİRME hissesi için kaldırıldı** (aktif split `configs/splits/current.json`'daki
  development listesi). **10 holdout hissesi izlenmez**; mevcut kod korumasının (tarama,
  analiz ve izleme araçlarında holdout sembol reddi) hepsi aynen kalır.
- **Birincil holdout testi** (30 sembol, 2026-10-01..2027-03-31, Holm H1+H2) planlandığı gibi
  kalır, ancak şu not düşülür: 2026-10-16'dan itibaren geliştirme sembollerinin pencere içi
  yön isabet oranı izlendiği için 30 sembollü test **geliştirme sembolleri bakımından kör
  değildir**. **E5** (yalnız holdout sembolleri) kör kalır.
- **Kurallar, eşikler ve spec'ler dondurulmuş kalır** (`FREEZE.md` hash'leri). Bir kural bu
  sayılar görüldükten sonra değiştirilirse, o kuralın holdout penceresi **değişiklik tarihinde
  yeniden başlar** ve bu tarih burada ve `TRIAL_LOG.md`'de kaydedilir.
- **Telegram çıktısı yoktur**: isabet izleme bildirim botuna, mesajlara ya da analiz notuna
  girmez; yalnız `data/interim/accuracy/<koşu tarihi>/` altına yerel dosya yazar.

**Araç.** `scripts/track_accuracy.py` (`src/fintr/accuracy/`; opt-in, `run_daily_pipeline.py
--accuracy`, varsayılan kapalı). Yürüyen (walk-forward) ve zaman-noktası güvenli: her sinyal
günü t için yalnız t ve öncesi veri; kayıtlı tahmin (`forward_log` / `scanner_daily`) varsa o
tercih edilir (`prediction_source`), yoksa yeniden kurulur (`reconstructed`). Sonuç: 5 işlem
günü (birincil), 1 işlem günü (ikincil), motorun giriş/çıkış ve getiri tanımıyla; maliyetli
isabet (2x10 bp); vadesi dolmamış `pending`. Taban oranı (koşulsuz yön oranı) ve Wilson %95 GA
ile; `train_window` (<= 2026-09-25, kurallar bu veriyle kuruldu) ve `post_train` ayrı raporlanır,
asla tek sayıda toplanmaz. Bilinen artık ileriye bakış (yeniden kurulan günlerde sonradan
revize edilmiş kayıtlar) `docs/accuracy.md`'de. Deneme kaydı: `TRIAL_LOG.md` T-31 (hesaptan
önce yazıldı).

**Değişmeyen.** Spec hash'leri, eşikler, tarayıcı kuralları, holdout planı
(`test_plan_v1.json`), holdout verisinin analizde kullanılmaması. Bu kayıt bir avantaj (edge)
iddiası değildir; izleme tanımlayıcıdır ve doğrulama yerine geçmez.
