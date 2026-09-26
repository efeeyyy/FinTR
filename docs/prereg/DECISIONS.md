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
