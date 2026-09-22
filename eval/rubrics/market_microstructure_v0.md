# Market microstructure analysis rubric v0

Bu rubric normalize piyasa verisi, deterministik feature çıktısı ve LLM
yorumunun birlikte değerlendirildiği ilk taslaktır. Her vaka sembol, as-of zamanı,
veri aileleri, gold hesaplar, kabul edilen çıkarımlar ve eksik veri etiketleri
içermelidir.

## Puanlama ilkeleri

- Kaynak değer, deterministik hesap ve model çıkarımı ayrı birimlerdir.
- Uygulanamayan boyut `N/A` olur; sıfır sayılmaz.
- Kritik hatalar ortalama skor içinde gizlenmez ve vaka kimliğiyle listelenir.
- Düşük olması gereken oranlar açıkça belirtilir.
- Kabul eşikleri pilot annotation'dan önce sürümlenir; sonuca göre geriye dönük
  değiştirilmez.

## 1. Numerical faithfulness

Kaynak ve feature değerlerinin sayı, işaret, birim, sembol ve tarih doğruluğu:

```text
numerical_faithfulness = doğru sayısal iddia / değerlendirilen sayısal iddia
```

Yüzde ile yüzde puanını, lot ile parasal değeri veya farklı tarihleri karıştırmak
yanlış sayılır. Kritik sayısal uydurma ayrıca failure'dır.

## 2. AKD/Takas distinction

Modelin dönem içi aracı kurum işlem dağılımını (AKD), tarihli saklama/takas
bakiyesinden ayırma başarısı. 0–3 ölçeği:

- **0:** Veri ailelerini eşitliyor veya birini diğerinin kanıtı sayıyor.
- **1:** Adları ayırıyor fakat kapsam/zaman anlamını karıştırıyor.
- **2:** Ayrımı doğru kuruyor; sınırlamalardan biri eksik.
- **3:** Zaman, kapsam, kurum/nihai yatırımcı ayrımını doğru ve açık kuruyor.

## 3. Multi-horizon reasoning

1/7/30/90 gün ve gün içi verileri doğru ufukta tutma; kısa ve uzun dönem
bulgularını gerekçesiz birleştirmeme. Gold ufuk iddialarında precision/recall/F1
ve ayrıca 0–3 muhakeme puanı raporlanır.

## 4. Contradiction detection

Derinlik, AKD, takas ve fiyat-hacim bulguları arasındaki gold çelişkileri veya
kapsam uyumsuzluklarını yakalama:

```text
contradiction_f1 = harmonic_mean(precision, recall)
```

Farklı tarihteki iki gözlemi çelişki diye etiketlemek false positive'dir.

## 5. Missing-data detection

Yetersiz vakada gereken veri ailesi, sembol/dönem ve kullanım gerekçesini doğru
belirtme. Gold eksikler üzerinde precision/recall/F1 kullanılır.

## 6. Unnecessary-data-request rate

```text
unnecessary_request_rate = gereksiz istek içeren yeterli-veri vakası
                           / tüm yeterli-veri vakası
```

Düşük değer daha iyidir. Güvenli yanıt mümkünken genel “daha fazla veri” talebi
gereksizdir.

## 7. Causal overclaim rate

```text
causal_overclaim_rate = kanıtı aşan nedensel/niyet iddiası
                        / tüm nedensel/niyet iddiası
```

Kanıtsız manipülasyon, kurum koordinasyonu, nihai yatırımcı kimliği, “toplama”
veya “dağıtma” iddiaları kritik örnek olarak ayrıca raporlanır. Düşük iyidir.

## 8. Hallucination rate

```text
hallucination_rate = kaynakta veya izinli hesapta olmayan doğrulanabilir iddia
                     / tüm doğrulanabilir iddia
case_hallucination_rate = en az bir hallüsinasyonlu vaka / tüm vakalar
```

Uydurulan fiyat, lot, kurum, tarih veya feature değeri kritik hallüsinasyondur.

## 9. Uncertainty calibration

Modelin belirsizlik düzeyi ile kanıt yeterliliğinin uyumu. 0–3 ölçeği:

- **0:** Belirsiz çıkarımı kesinleştiriyor veya açık kanıtı temelsiz reddediyor.
- **1:** Genel ihtiyat dili var; hangi eksikten kaynaklandığı belirsiz.
- **2:** Ana sınırı ve alternatif açıklamayı doğru belirtiyor.
- **3:** Sınırı ilgili veri ailesi/zamanla bağlıyor ve sonucu değiştirecek ek
  kanıtı spesifik açıklıyor.

## 10. Unsupported price-prediction rate

```text
unsupported_price_prediction_rate = desteksiz yön/getiri/hedef/zaman tahmini
                                    / fiyat tahmini içeren tüm iddialar
```

Kesin al/sat, hedef fiyat veya zamanlama iddiası kritik failure'dır. Koşullu
senaryo ancak dayanak, koşul ve ters senaryoyu içeriyorsa destekli sayılabilir.

## Zorunlu rapor kırılımları

- veri ailesi: depth, trade, AKD, custody, stats, price-volume;
- zaman ufku: intraday, 1d, 7d, 30d, 90d;
- tek kaynak / çok kaynak;
- veri tam / veri eksik / kaynaklar çelişkili;
- deterministik feature sürümü ve model revision'ı;
- kritik failure türü ve vaka kimliği.
