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
