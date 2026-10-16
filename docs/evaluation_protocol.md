# Eğitim öncesi değerlendirme protokolü v0

Bu aşamada gerçek gold vaka veya ölçülmüş model baseline'ı yoktur. Aşağıdaki
sıra, veri izni ve ayrılmış evaluation seti netleştiğinde uygulanacaktır:

AKFIS/2026-09-23 pilot vakası prompt ve çıktı geliştirmesinde kullanılmıştır;
bağımsız held-out, gold veya baseline örneği değildir. Bu vaka training veya
nihai evaluation verisine dahil edilmez. Bağımsız gold vakalar ve ölçülmüş
baseline'lar henüz oluşturulmamıştır.

1. Değerlendirme vakalarını kaynak, sembol, zaman, veri ailesi, ufuk ve
   eksik/çelişkili veri durumu ile kaydet. Manifestte `split=eval` kullan;
   train/validation/retrieval ve prompt seçimine bu vakaları sokma.
2. Model yanıtını görmeden, kaynak değerleri, izinli hesapları, kabul edilebilir
   çıkarımları ve kritik hataları bağımsız insan incelemesiyle gold olarak
   dondur. İkinci gözden geçirme ve anlaşmazlık çözümünü kaydet.
3. Sabit model revision'ı, prompt sürümü, kod commit'i, seed/decoding ayarı ve
   donanım ile baseline yanıtlarını üret. Gold'u modele veya prompt'a verme.
4. Yanıt başına `market_microstructure_v0` rubric'ini insan gözetiminde
   işaretle. `fintr.evaluation.scoring.score_assessments` yalnız bu işaretlerin
   aritmetiğini yapar; metinden otomatik doğruluk veya nedensellik kararı vermez.
5. Sayısal oranların pay/paydasını, uygulanamayan boyutları (`null`) ve vaka
   kimliğiyle kritik hataları birlikte raporla. Yalnız ortalama skorla kabul
   kararı verme. Rubric eşiklerini sonuçlara bakıp geriye dönük değiştirme.

Puanlayıcı sayısal sadakat, AKD/Takas ayrımı, çoklu ufuk, çelişki, eksik veri,
gereksiz veri talebi, kanıtsız nedensellik, uydurma, belirsizlik ve desteksiz
fiyat tahmini boyutlarını toplar. F1 için hiç pozitif/prediction yoksa sonuç
`null`; gold pozitif olup tahmin yoksa F1 sıfırdır. Vaka başına kritik failure
kodları ortalamadan ayrı kalır.

Henüz eksik: gerçek bağımsız gold vakaları, baseline yanıtları ve sürümlü
metric artefact'ı. Bu dosya veya sentetik testlerin başarısı eğitim
kapısının geçtiği anlamına gelmez.

## Manifest, annotation ve leakage guard (araç, veri değil)

`src/fintr/evaluation/manifest.py` vaka manifesti (`fintr_eval_manifest_v1`)
ve annotation kaydı (`fintr_eval_annotation_v1`) biçimlerini strict tip
kontrolüyle tanımlar ve ayrıştırır. Vaka alanları: `case_id`, `split`
(yalnız `"eval"`), `symbol`, `data_families`, `observation_period`
(start/end), `horizons`, `source` (yol + SHA-256 + capture zamanı),
`permission` (kullanıcı beyanlı; `independently_verified` her zaman
`false` olmalı — bu araç izin/lisans doğrulaması yapmaz), `leakage_group`
(`symbol|start|end`, yeniden hesaplanıp doğrulanır) ve `notes`. Annotation
kaydı `market_microstructure_v0` rubric'inin 10 boyutunun tümünü kapsamalı;
her boyut `applicable` bayrağı taşır (N/A boyut içerik taşımaz, uygulanabilir
boyut en az bir gold fact içerir), ikinci gözden geçirme/anlaşmazlık çözümü
alanları vardır ve `frozen=true` iken `checksum`
(`compute_annotation_checksum`) içeriğin değişmediğini doğrular.

`src/fintr/evaluation/leakage_guard.py`, `src/fintr/evaluation/dev_registry.json`
içindeki (sembol + tarih aralığı, mali değer taşımayan) geliştirme-amaçlı
kayıtlara karşı manifesti denetler. Sembol kontrolü şirket seviyesindedir:
registry'de AKFIS 2026-09-23 pilotu (`data/interim/pilot/`) ve AKFIS
2026-09-08..2026-09-25 period capture'ı (`data/interim/period_analysis/AKFIS/`)
kayıtlı olduğu için, tarihi ne olursa olsun AKFIS sembolündeki her vaka
`development_symbol` nedeniyle reddedilir; registry'nin `start`/`end`
alanları yalnızca hangi materyalin kullanıldığını belgeler, tarih sınırı
olarak kullanılmaz. Bu, AGENTS.md'nin sızıntı kurallarıyla (doküman ailesi,
şirket, rapor dönemi, olay bazında ayır; yalnız satır bazlı split yapma)
ve takas/saklama verisinin T+2 ile hafta sonuna yayılan mutabakatıyla
uyumludur: kayıtlı bir geliştirme döneminden birkaç gün sonraki aynı
sembol vakası da o dönemin bilgisini taşıyabilir. Ayrıca bilinen geliştirme
yolu (`data/interim/pilot/`, `data/interim/period_analysis/`), yinelenen
`case_id`, yinelenen capture checksum'ı veya yinelenen `leakage_group`
vakayı sabit, payload'suz bir nedenle reddeder.

`scripts/validate_eval_manifest.py` bir manifesti (+ opsiyonel annotation
dizinini) bu biçimlere ve leakage guard'a karşı doğrulayıp kısa bir rapor
basar; ihlalde çıkış kodu sıfırdan farklıdır. Gerçek eval verisi git-ignore'lu
`data/eval/` altında yaşar; `eval/cases/` yalnızca sentetik, açıkça örnek
değerler taşıyan bir şablon çifti içerir (bkz. `eval/cases/README.md`).

Bu araçların geçmesi insan işini bitirmez: gerçek gold vakaların bağımsız
insan annotation'ı, ikinci gözden geçirme, baseline yanıtları ve sürümlü
metric artefact'ı hâlâ insan tarafından üretilmelidir.

## Dondurulmuş BIST 30 hisse ayrımı ve dışlama listesi (kodla zorunlu)

**Etkin ayrım yalnızca `configs/splits/current.json` işaretçisiyle çözülür**
(`fintr_current_split_v1`; şu an `configs/splits/bist30_2026_holdout_v2.json`).
Hiçbir script'te ayrım dosyası veya dışlama dosyası seçen bir bayrak yoktur;
yollar repo köküne göre mutlaktır (çalışma dizininden bağımsız).
`fintr.evaluation.symbol_split.resolve_active_split` kapalı-güvenli çalışır:
işaretçi yoksa/bozuksa, gösterdiği dosya yoksa, ayrım `frozen: false` ise
veya `superseded_by` alanı taşıyorsa (değeri `null` olsa bile) hata verir —
koruma sessizce kapanmaz. Eski bir ayrım (ör. v1) yalnız denetim için
`load_symbol_split(..., require_active=False)` ile okunabilir. Etkin ayrım
dışlanmış bir hisse içeriyorsa (`split_contains_excluded_symbol`) de hata
verilir. Kaynak evren/dışlama dosyalarının ayrımda kayıtlı SHA-256'ları
yeniden doğrulanmaz (kayıtlı provenance'tır); raporlanan SHA-256, uygulanan
ayrım dosyasının kendi baytlarınındır.

Her analiz/çalışma/doğrulama çıktısı `split_guard` bloğunu
(`fintr_symbol_guard_report_v1`) taşır ya da metin olarak basar:
`split_file`, `split_file_sha256`, `split_id`, `split_pointer`,
`exclusions_file`, `exclusions_file_sha256`, `excluded_symbols`,
`guard_bypassed`, `bypass_warning`, `bypassed_holdout_symbols`.

Holdout kuralları (`holdout_symbols`):

| Araç | Varsayılan | `--all`/toplu | `--no-split-guard` |
| --- | --- | --- | --- |
| `scripts/run_study.py --split train` | spec'in `symbols`'ında veya `--data`'da holdout hisse → red | — | holdout kontrolü kapalı |
| `scripts/run_study.py --split test` | yalnız `frozen: true` spec; spec'in boş olmayan `symbol_holdout`'u ayrımın holdout'unun alt kümesi olmalı; `--data` yalnız holdout hisse; motor yalnız `symbol_holdout`'u değerlendirir (`symbol_scope: holdout_only`) | — | holdout kontrolü kapalı (spec'in beyan ettiği tüm semboller) |
| `scripts/analyze_period.py` | holdout capture → red | `skipped_holdout` | holdout işlenir |
| `scripts/custody_flow.py` | holdout capture → red | — | holdout işlenir |
| `scripts/validate_eval_manifest.py` | holdout listesinde olmayan her vaka → `not_in_holdout_split` | — | bayrak yok |
| `scripts/fetch_price_history*.py`, `scripts/live_collector_smoke.py` | **engellenmez** — holdout verisi toplanabilir | — | — |

`--no-split-guard` yalnız BIST 30 dışı keşif içindir. Kullanıldığında
stderr'e uyarı basılır, metin çıktısı `!!! UYARI: --no-split-guard AKTİF`
satırıyla başlar ve JSON'da `guard_bypassed: true`, `bypass_warning` ve bu
koşuda koruma kapalıyken işlenen holdout hisseler
(`bypassed_holdout_symbols`) yazılır. Bayrak ayrımın yüklenmesini atlamaz
(bozuk ayrım yine hata verir) ve dışlamaları asla kapatmaz.

Dışlama kuralları (`configs/exclusions/symbols.json`,
`fintr_symbol_exclusions_v1`): dışlanan hisse **her yerde, geçersiz kılma
seçeneği olmadan** reddedilir veya atlanır — `run_study` (`--data` veya
spec'in beyan ettiği semboller), `analyze_period` (tekli: red; `--all`:
`skipped_excluded`), `custody_flow`, `validate_eval_manifest`
(`excluded_symbol`), `fetch_price_history.py` (dry-run dahil, URL kurulmadan
red), `fetch_price_history_batch.py` (`skipped_excluded`, istek yok) ve
`live_collector_smoke.py` (tek hisse: initData istenmeden ve bağlantı
açılmadan red; çoklu hisse: `skipped_excluded`). Dosya yoksa bu da hatadır
(`exclusions_file_missing`). Bir kaydın `retained_data` notu yalnız bilgi
amaçlıdır (hangi yerel verinin neden tutulduğunu kaydeder); kodda hiçbir
istisna açmaz.

Testler: `tests/test_symbol_guard.py` (yalnız geçici dizinde sentetik ayrım,
işaretçi, dışlama, capture, manifest ve evren dosyaları).

## İşlem maliyeti duyarlılığı (karar girdisi değil)

Holdout testinin birincil analizi donmuş maliyetle (10 bps/taraf) çalışır.
`docs/prereg/DECISIONS.md` 2026-10-16'ya göre (`docs/prereg/TRIAL_LOG.md` P-03,
`configs/studies/prereg/cost_sensitivity_v1.json`, hash'i DECISIONS.md'de
kayıtlı ve çalıştırıcıda doğrulanır) aynı tek test yürütmesi, aynı dondurulmuş
hesabı yalnız maliyeti 0/5/20/30 bps/taraf yaparak yeniden çalıştırır ve ayrı
artefaktlara yazar (`scripts/run_holdout_test.py` varsayılan açık;
`scripts/run_study.py --cost-sensitivity-config`). Bu sonuçlar tanımlayıcıdır:
Holm kararına, hipotez sonucuna ve birincil rapora girmez; model seçimi, eşik
veya spec değişikliği için kullanılamaz. Testler: `tests/test_cost_sensitivity.py`
ve `tests/test_holdout_test_runner.py` (yalnız sentetik fikstür).
