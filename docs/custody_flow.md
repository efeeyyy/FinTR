# Custody-flow: eksik AKD kodu politikası ve tahmini bireysel lot (custody_flow_v3)

`fintr.features.custody_flow.compute_custody_residual` bir server-tarihindeki
AKD net listesinde bir kodun eksik olmasını iki farklı politikayla ele alır.
Bu doküman `missing_akd_policy` parametresini ve dayandığı kararı kaydeder.

## Politikalar

`MISSING_AKD_POLICIES = ("zero", "unknown")`

- **`zero`** (varsayılan) -- anchor server-tarihi (`a`) için AKD paneli
  capture'da mevcutsa ama belirli bir kod o panelde listelenmemişse, o
  kodun AKD net değeri o tarih için `0` sayılır.
- **`unknown`** (önceki davranış) -- panel mevcut olsa da olmasa da,
  listelenmeyen kod için AKD net değeri `None`'dur (residual `None` olur).

İki politika da şu durumda **aynı** davranır: anchor tarihi için AKD paneli
capture'da **hiç yok** ise (o tarihe ait `daily_akd_net` girdisi yoksa),
residual `None`'dur. `zero` politikası panelin var olduğu ama kodun
listelenmediği duruma özgüdür; boş/eksik bir günün tamamını asla sıfırla
doldurmaz.

Custody lot (`daily_custody_lots`) girdileri bu politikadan etkilenmez: bir
kod custody'de o tarihte veya önceki tarihte listelenmemişse residual her
zaman `None`'dur -- takas/saklama değeri hiçbir zaman 0 kabul edilmez.

## Formül

```text
residual(code, t) = (lot[code, t] - lot[code, p]) - akd_net(code, a)
```

`akd_net(code, a)`:
- kod panelde listeliyse: listelenen değer.
- panel `a` için capture'da yoksa: tanımsız -> residual `None` (politika
  farketmez).
- panel `a` için var ama kod listelenmemişse:
  - `zero`: `0`
  - `unknown`: tanımsız -> residual `None`

Her `CodeResidual` satırı `akd_defaulted: bool` alanı taşır: `True` yalnızca
AKD girdisi bu satırda `zero` politikasıyla `0`'a tamamlandığında. Her
`CustodyResidualResult`, kullanılan `missing_akd_policy` ve
`formula_version` (`custody_flow_v3`) değerlerini taşır.

## Karar ve kanıt (2026-09-26)

Proje sahibi Efe'nin kaydı: bir günün AKD listesinde bir kurum/kod
görünmüyorsa, o gün için net akışının 0 olduğu kabul edilir (`zero`
politikası varsayılan yapıldı).

Kanıt: iki kayıtlı AKFIS capture'ında da, listelenen kurumların günlük
`net_size` toplamı **her** server-tarihinde tam olarak 0'a denk geliyor
(period_features `net_sum_zero` kontrolü tüm günlerde geçti). Bu, listenin
kesik/eksik (truncated) olmasıyla **uyumsuz** bir sonuçtur -- liste kesik
olsaydı toplamın rastgele bir kalıntıya sahip olması beklenirdi. Bu gözlem,
listenin o gün için kapsayıcı (exhaustive) olduğunu ve listelenmeyen bir
kodun o gün net 0 işlem gördüğünü düşündürür.

Bu kanıt ve karar `fintr.features.custody_flow.MISSING_AKD_ZERO_EVIDENCE`
sabitinde ve modül docstring'inde de tutulur; CLI (`scripts/custody_flow.py
--missing-akd-policy`) JSON çıktısında hem seçilen politikayı hem de bu
kanıt notunu (`zero` seçiliyken) taşır.

Bu bir çıkarımdır, doğrulanmış bir kural değildir: capture kapsamının
dışındaki dönemler veya farklı bir sağlayıcı/format için aynı varsayım
otomatik olarak geçerli sayılmamalıdır. Belirsiz kalan bir girdi hâlâ
`unknown` politikasıyla `None` olarak işaretlenebilir.

## Tahmini bireysel/kurumsal lot serisi (`compute_estimated_retail_lots`)

`fintr.features.custody_flow.compute_estimated_retail_lots`, sağlayıcının
verdiği `retail_pct` / `institutional_pct` yüzdesini, **aynı** saklama
tarihindeki toplam saklama lotuna uygulayarak bir lot-ölçeğinde tahmin
üretir. Bu bir sağlayıcı raporu değil, **hesaplanan** bir değerdir.

### Formül

```text
total(t)                      = Σ_c custody_lots(c, t)   (t'nin tüm saklama kodları, gecikme/ileri taşıma yok)
estimated_retail_lots(t)       = retail_pct(t) / 100 * total(t)
estimated_institutional_lots(t)= institutional_pct(t) / 100 * total(t)
```

Her ikisi de `None` olur eğer:
- ilgili yüzde (`retail_pct` veya `institutional_pct`) o tarih için null ise, VEYA
- o tarihin saklama paneli capture'da hiç yoksa (`total(t)` tanımsız).

İleri taşıma (forward-fill) yapılmaz; her tarih kendi başınadır.

### Etiket (`RETAIL_LOT_LABEL`, birebir)

> Tahmini bireysel lot (hesaplanan): sağlayıcı retail_pct × aynı saklama
> tarihindeki toplam saklama lotu / 100. Bireysel/kurumsal sınıflaması
> sağlayıcıya aittir; yöntem belgesi doğrulanmadı.

### Doğrulama durumu

`validation_status = "provider_method_consistent_single_symbol_study"`

Bu durum, çevrimdışı bir araştırma notuna işaret eder:
`data/interim/research/retail_pct_validation.md` (`RETAIL_LOT_VALIDATION_STUDY_FILE`
sabitinde dosya adı olarak tutulur; notun sayıları bu koda veya bu belgeye
kopyalanmamıştır -- yalnızca dosya adı referans olarak taşınır). Notun
yöntem bölümüne göre, sağlayıcının `retail_pct` alanı aynı saklama tarihinin
(gecikme 0) toplamına göre bir pay olarak tutarlı bulunmuştur, ama:

- **Yalnızca tek sembol ve tek capture dönemi** üzerinde test edilmiştir;
  başka sembol/dönem için genel bir garanti değildir.
- Saklama lot ölçeğindeki bir değişiklik (örn. bir kurumsal aksiyon/bölünme)
  mutlak lot seviyelerini değiştirir ama -- bu nota göre -- bireysel/kurumsal
  **oranını** kendiliğinden değiştirmez, çünkü tahmin toplamın bir yüzdesidir
  (pay da payda da aynı ölçekle büyür/küçülür).

### Geçici (provisional) işaretleme

`provisional_last_custody_days` (varsayılan `1`, `fintr.features.period`
ile aynı politika ve varsayılan): capture'ın en son bu kadar server-tarihi
`provisional=True` işaretlenir, çünkü bu tarihlerin saklama bakiyeleri daha
sonra revize edilebiliyor (period report'taki `check_custodian_diff` ile aynı
kural). `0` verilirse hiçbir tarih işaretlenmez. Kullanılan değer sonuçta ve
CLI çıktısında (`provisional_last_custody_days`) açıkça taşınır.

CLI: `scripts/custody_flow.py` çıktısı bu seriyi her zaman
`estimated_retail_lots` anahtarı altında içerir (ek bir dosya gerekmediği
için code-set'in aksine opsiyonel değildir); `--provisional-last-custody-days`
bayrağıyla eşik değiştirilebilir.
