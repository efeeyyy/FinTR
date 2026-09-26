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

Henüz eksik: gerçek bağımsız gold vakaları, bunlara bağlı veri manifesti,
annotation kayıt biçimi, rubric'in zorunlu aile/ufuk kırılımları, baseline
yanıtları ve sürümlü metric artefact'ı. Bu dosya veya sentetik testlerin
başarısı eğitim kapısının geçtiği anlamına gelmez.
