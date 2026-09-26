# Kalıcı kayıt seçeneği ve ilk bellek içi pilot

Durum: 2026-09-24. Genel canlı koleksiyon kalıcılığı ertelidir. Kullanıcının
yerel kayıt onayı üzerine yalnız AKFIS/2026-09-23 için doğrulanmış beş aileden
üretilen bounded pilot context'e açık opt-in kayıt yolu eklendi. Vaka prompt ve
çıktı geliştirmesinde kullanıldığı için held-out eval değildir; eğitim ve nihai
değerlendirme için uygun değildir. Gerçek capture bu uygulama sırasında
çalıştırılmadı.

## Mevcut kanıt

- Kullanıcının paylaştığı tek canlı denemede brokerage, AKD, günlük Takas,
  Takas dağılımı ve Takas istatistiği yanıtları aynı collector çağrısında
  normalize edildi; `LiveCollectionEvidence` bellekte doğrulandı.
- Collector `DispatchResult` nesnelerini kalıcı yazmaz; ham WebSocket çerçeveleri
  ve kontrol mesajları doğrudan saklanmaz. Ayrı capture writer yalnız doğrulanmış
  `LiveAnalysisContext` nesnesini kabul eder.
- İzin metadata'sı 2026-09-24 tarihli kullanıcı beyanı olduğunu kaydeder; kaynak
  lisansı veya bağımsız kullanım hakkı doğrulanmış sayılmaz.

## Kararlaştırılan ilk pilot

Tek bir açıkça başlatılan collector çağrısı, yalnız beş doğrulanmış yanıt ailesi
ve AKFIS için 2026-09-23.
Başarılı gönderim ve normalize yanıtlar ile
`build_live_collection_evidence` kontrolünden sonra typed yanıtlar bellek içinde
LLM bağlamına dönüştürülür. Sayılar, birimler, kaynak ve tarih kapsamları
ayrı kalır. İlk pilotta otomatik tekrar veya sürekli abonelik yoktur.

`--capture-pilot-context` varsayılan olarak kapalıdır ve yalnız aynı beş request,
AKFIS/2026-09-23, evidence, context ve prompt doğrulamaları geçince sabit dosyayı
`data/interim/pilot/live_context_AKFIS_2026-09-23.json` adıyla no-overwrite olarak
yazar. Artefact schema v2 `classification=pilot/development`, `split=pilot`,
`heldout_eligible=false`, `training_allowed=false` ve `evaluation_allowed=false`
değerleriyle context checksum'ı, limitler, izin beyanı, kaynak/aile ve dönüşüm
soyunu taşır; gold label veya model çıktısı içermez. Eski
`--capture-eval-context` seçeneği bağlantı kurulmadan reddedilir. Önceden eval
diye işaretlenmiş dosyanın orijinal JSON baytları değiştirilmeden taşınmış, eski
ve yeni yolu ile SHA-256 değeri
`data/interim/pilot/live_context_AKFIS_2026-09-23.classification-correction.json`
sidecar düzeltmesine yazılmıştır.

Ham frame, `initData`, bağlantı URL'si, `welcome`/`hb`, favori ve portföy
mesajları analiz bağlamına girmez. Bellek içi bağlam oluşturmak, daha sonra
yeniden üretilebilir bir eğitim veri kümesi hazırlamakla aynı iş değildir.

## Sınırlar ve sonraki kararlar

| Karar | Dar pilot önerisi | Durum |
|---|---|---|
| Sembol ve tarih | AKFIS, 2026-09-23 | İlk pilot için seçildi |
| Veri aileleri | Doğrulanmış beş yanıt ailesi | İlk pilot için seçildi |
| Saklama süresi | Yerel pilot, elle yönetilir | Otomatik silme yok |
| Kullanım | Yalnız bounded development pilot capture | Held-out eval/eğitim dışı |
| İzin kaydı | 2026-09-24 tarihli kullanıcı beyanı | Bağımsız lisans doğrulaması yok |

Bu dar dosya mevcut eğitim manifestine kabul edilmiş artifact değildir; provider
lisansı, yeniden kullanım hakkı, gold vaka, bağımsız puanlama, split sızıntısı ve
genel koleksiyon saklama politikası ayrıca ele alınmalıdır. Gerçek canlı deneme
Mini App oturumunun kapanabileceği uyarısıyla ayrıca açıkça başlatılır. Tek
capture, gold değerlendirme veya model ağırlığı eğitimi sayılmaz.
