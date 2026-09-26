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
