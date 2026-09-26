# Deterministik piyasa özellikleri

Bu formüller LLM promptundan bağımsız hesaplanır. Oran sonuçları `Decimal`
olarak tutulur. Payda sıfırsa sonuç `None` olur; sıfır uydurulmaz.

## Derinlik

```text
total_bid_lots = sum(all bid lots)
total_ask_lots = sum(all ask lots)
bid_ask_imbalance = (total_bid_lots - total_ask_lots)
                    / (total_bid_lots + total_ask_lots)
top5_bid_lots = sum(lots at five highest bid prices)
top5_ask_lots = sum(lots at five lowest ask prices)
spread = best_ask - best_bid
midpoint = (best_ask + best_bid) / 2
spread_pct = spread / midpoint * 100
```

Dengesizlik `[-1, 1]` aralığındadır. Tek snapshot emir iptali ve gerçekleşme
bilgisi içermediği için kalıcı yön sinyali değildir.

## AKD

`top_buyers` pozitif `net_lots` değerine göre azalan; `top_sellers` negatif
`net_lots` değerine göre artan ilk beş kurumdur.

```text
top5_buy_concentration = largest_5(buy_lots).sum / all_buy_lots.sum
top5_sell_concentration = largest_5(sell_lots).sum / all_sell_lots.sum
broker_cost_vs_market_price_pct = (average_cost - market_price)
                                  / market_price * 100
```

Piyasa fiyatı sıfır/eksik veya kurum maliyeti eksikse maliyet farkı `None` olur.
Yoğunlaşma oranları `[0, 1]` ölçeğindedir.

## Takas/saklama

Dağılım `values` sırası sabittir:
`[latest, previous_day, previous_week, previous_month, previous_3_months]`.

```text
change_1d  = latest - previous_day
change_7d  = latest - previous_week
change_30d = latest - previous_month
change_90d = latest - previous_3_months
top5_custody_concentration = largest_5(custody_value).sum
                             / all_custody_value.sum
```

Kurum persistence hesabında sıfır `daily_net` günleri yön gözlemi sayılmaz:

```text
persistence = max(positive_day_count, negative_day_count) / nonzero_day_count
direction = buy | sell | mixed
```

Bu metrik akış yönünün gözlenen günlerdeki tutarlılığıdır; aynı nihai yatırımcıyı
veya gelecek fiyat yönünü göstermez.

## Yatırımcı istatistikleri

Her seri tarih sırasına konur. Değişim, sağlanan aralıktaki son değerden ilk
değerin çıkarılmasıdır:

```text
retail_share_change = latest_retail_percentage - earliest_retail_percentage
institutional_share_change = latest_institution_percentage
                             - earliest_institution_percentage
investor_count_change = latest_investor_count - earliest_investor_count
```

İkiden az gözlem varsa değişim `None` olur. Yüzde serilerindeki sonuç yüzde puan,
yatırımcı sayısındaki sonuç kişi/adet farkıdır.

## Canlı modeller (`live_features_v1`)

`fintr.features.live`, WebSocket `Live*` modelleri (`fintr.data.models`)
üzerinde çalışır; yukarıdaki AKD/Takas/yatırımcı istatistiği bölümleri eski
snapshot modelleri (`AKDSnapshot`, `CustodianSnapshot`,
`CustodyDistributionSnapshot`, `CustodyStats`) için ayrı ve değişmeden kalır.
Sonuçlar `fintr.inference.live_context.build_live_analysis_context` içinde
`computed_features` altında, sağlayıcının `as_received` alanlarından ayrı bir
bölümde taşınır; **her zaman kırpılmamış tüm satır/nokta kümesi** üzerinden
hesaplanır (bağlamdaki `row_selection` limitleri yalnız `as_received`
satırlarını kırpar).

```text
live_akd_positive_net_sum        = sum(net.size for net.size > 0)
live_akd_negative_net_sum        = sum(net.size for net.size < 0)
live_akd_net_sum_as_received     = sum(net.size for all rows)
                                    (bütünlük gözlemi, sıfır uydurulmaz)
live_akd_top5_positive_net_share = sum(en büyük 5 pozitif net.size)
                                    / positive_net_sum
                                    (positive_net_sum == 0 ise None)
live_akd_top5_negative_net_share = sum(en büyük 5 |negatif net.size|)
                                    / |negative_net_sum|
                                    (negative_net_sum == 0 ise None)

live_custodian_total_value                = sum(value)
live_custodian_top5_value_share           = sum(en büyük 5 value) / total_value
                                             (total_value == 0 ise None)
live_custodian_percentage_sum_as_received = sum(percentage)
                                             (bütünlük gözlemi, sıfır uydurulmaz)

live_distribution_change = latest_period_value - other_period_value
                            (en güncel dönem tarihe göre bulunur, tuple
                            sırasına göre değil; iki taraftan biri None ise
                            sonuç None; dönem sayısı ile değer sayısı
                            uyuşmazsa ValueError)

live_stats_series_change = son null-olmayan değer - ilk null-olmayan değer
                            (noktalar tarihe göre sıralanır; 2'den az
                            null-olmayan nokta varsa None; kullanılan ilk/son
                            tarih ayrıca raporlanır)
```

Bu formüller `formula_version="live_features_v1"` ile etiketlenir ve
`computed_features.formulas` altında context'e aynen kopyalanır; LLM sonucu
yeniden hesaplamak yerine bu değerleri kullanmalıdır.

## Dönem paketi kontrolleri (`period_features_v2`)

Kaydedilmiş `fintr_period_analysis_capture_v1` dosyası üzerinde çalışır
(`fintr.data.period_capture`, `fintr.features.period`). Yükleyici paket
hash'ini yeniden hesaplar; uyuşmazsa analiz yapılmaz. `t`, sunucu tarihlerinin
artan sıradaki indeksidir; takvim günü değil işlem gözlemi adımıdır. Değerler
sağlayıcının birimiyle kalır.

```text
akd_daily_sum[code]   = sum_t daily_akd.net_size[code, t]   (listelenen günler)
akd_reconciliation    = match | mismatch | missing_in_daily | missing_in_period
                        (akd_daily_sum[code] vs get_akd_period.net.size[code])
diff_internal         = last_value - first_value == difference
diff_vs_daily         = first_value vs lot_count[code, ilk sunucu tarihi]
                        last_value  vs lot_count[code, son sunucu tarihi]
                        (o gün listelenmeyen kod: absent_in_daily_panel, sıfır değil)
flow_run              = aynı işaretli, ardışık listelenmiş günler; listelenmeyen gün
                        diziyi keser
custody_change[c,t+L] = lot_count[c, t+L] - lot_count[c, t+L-1]
lag_exact(L)          = custody_change[c, t+L] == akd.net_size[c, t]
lag_sign(L)           = sign(custody_change[c, t+L]) == sign(akd.net_size[c, t])
```

Gecikme eşleşmesi zamanlama gözlemidir; sağlayıcı yöntemini, nedenselliği veya
nihai yatırımcıyı kanıtlamaz. `t + L` gözlemi pakette yoksa ilgili AKD günleri
`akd_dates_without_later_custody` altında ayrıca listelenir.

### Günlük piyasa proxy'si (`daily_market_proxy`, doğrulanmamış varsayım)

Kaydedilmiş dönem paketlerinde resmi bir fiyat/hacim serisi yoktur. Bu bölüm,
her günün AKD satırlarındaki `total_size` ve `total_cost` alanlarından
türetilmiş, **doğrulanmamış bir hipoteze dayanan proxy**'dir; etiketi
`derived_proxy_not_official_ohlc`'dır ve resmi bir OHLC/hacim serisinin yerini
tutmaz.

Varsayım (doğrulanmamış): bir kurumun günlük AKD satırındaki `total_size`
alanı o kurumun o günkü alış+satış lot toplamı, `total_cost` ise o kurumun o
günkü ortalama işlem fiyatıdır; dolayısıyla `Σ_kurum total_size = 2 × işlem
gören lot`. `total_size` ve `total_cost`, `net_size`'ın geldiği aynı
`daily_akd_flow_in_provider_order` satırından okunur; bir kod o gün biri için
listelenmişse üçü için de listelenmiştir.

```text
sum_total_size[t]        = sum_code total_size[code, t]
implied_traded_lots[t]   = sum_total_size[t] / 2
                            (yalnızca net_within_total ve total_size_even o
                            gün geçtiyse anlamlıdır)
vwap_proxy[t]             = sum_code(total_size[code, t] * total_cost[code, t])
                            / sum_total_size[t]
                            (sum_total_size[t] == 0 veya o gün satır yoksa None)

net_sum_zero[t]          = sum_code net_size[code, t] == 0
total_size_even[t]       = sum_total_size[t] çift mi
net_within_total[t]      = o gün listelenen her kod için |net_size| <= total_size
vwap_within_range[t]     = min(total_cost o gün) <= vwap_proxy[t] <= max(total_cost o gün)
                            (total_size > 0 olan kodlar üzerinden)
```

Bir sunucu tarihi `daily_akd_flow_in_provider_order`'da hiç yoksa o tarihin
tüm proxy değerleri ve kontrolleri `None`'dır (sıfır uydurulmaz, kontrol
"geçmedi" de sayılmaz — değerlendirilmemiş olarak işaretlenir). Rapor, her
kontrol için değerlendirilen gün sayısı, geçen gün sayısı ve başarısız gün
sayısını `daily_market_proxy.check_summary` altında taşır. `scripts/
analyze_period.py` metin çıktısı yalnızca bu geçti/toplam sayılarını gösterir;
tek tek `total_size`/`total_cost` değerlerini listelemez. Toplu mod indeksi
(`--all`) her kayıt için dört kontrolün tamamının geçip geçmediğini tek bir
`proxy_checks_passed` bayrağıyla özetler.

`fintr_period_analysis_handoff_v2` paketi (`get_brokerages` dahil dörtlü
profil) `brokerage_directory` bölümünde o anki, tarihsiz ve yalnız tam kod
eşleşmesiyle kullanılan bir kurum listesi taşır (`code`, `title`,
`short_title`); nihai yatırımcıyı belirtmez. Rapor, dönem AKD üst listeleri,
takas farkı üst listeleri, kurum akış dizileri ve takas farkı istisnalarındaki
her kodun yanına `{status, title, short_title}` biçiminde bir `label` ekler.
`status` değerleri: `resolved`, `unresolved`, `ambiguous` (birden çok çelişen
ad varsa isim seçilmez) ve eski `fintr_period_analysis_handoff_v1` paketlerinde
(kayıtta kurum dizini hiç yok) `directory_not_in_capture`. Takas (custodian)
kodları da aynı dizine karşı aranır; dizinde yoksa `unresolved` kalır, elle
eşleştirme yapılmaz. Metin çıktısında yalnızca `resolved` durumundaki kodlar
`KOD (short_title)` biçiminde gösterilir.

### Fiyat/hacim eşleştirmesi (`price_join_v1`, resmi olmayan kaynak)

`fintr.features.price_join`, kaydedilmiş bir fiyat geçmişi capture'ını
(`fintr.data.price_history.load_price_history_capture`, bkz.
`docs/price_history.md`) dönem paketinin sunucu tarihlerine ve
`daily_market_proxy` çıktısına hizalar. İki taraf da bağımsız olarak
doğrulanmamıştır; bu bölüm bir nedensellik iddiası değildir ve fiyat yönü
çıkarımı yapmaz.

Her sunucu tarihi için: o tarihe ait bar varsa ham `close`/`volume` ile
`adjusted_close` ayrı alanlarda tutulur;
`vwap_within_bar_range = bar.low <= vwap_proxy <= bar.high`;
`volume_ratio = implied_traded_lots / bar.volume` (Decimal, hacim 0/None ise
`None`) ve bunun `volume_ratio_tolerance` parametresi içinde olup olmadığı
(varsayılan `0.5`; "lot" birimi AKD/takas tarafında fiyat kaynağının hacim
birimiyle aynı olmak zorunda değildir, bu yüzden geniş bir bant yalnızca
büyük ölçekli tutarsızlıkları işaretler). Bar yoksa o tarih
`missing_server_dates` altında listelenir.

Dönem paketinde karşılığı olmayan (price-only) tarihler üçe ayrılır:
`placeholder` (hacim 0/None ve `open == high == low == close` — bir borsa
tatili işaretleyici barının belgelenen şekli), `no_data_bar` (dört OHLC alanı
da `None`) ve `unexpected_trading_bar` (diğerleri). Placeholder barlar asla
gerçek bir işlem gözlemi sayılmaz.

`adjustment_ratio = adjusted_close / close` barın tarihinde hesaplanır; bu
oranın bir önceki fiyatlı bardan `adjustment_ratio_epsilon` parametresinden
(varsayılan `0.0005`, göreli) daha fazla değiştiği tarihler
`adjustment_boundary_dates` altında yalnızca **tarih olarak** listelenir
(oran değerleri raporlanmaz). Bu, takas toplamlarında bazı tarihlerde
görülen ölçek sıçramalarıyla ilişkilendirilebilir bir gözlemdir; nedeni
çıkarılmaz.

`scripts/analyze_period.py`, tek kayıt modunda `--price-history PATH` ile
belirtilen dosyayı; `--all` modunda ise
`--price-history-root` (varsayılan `data/interim/price_history`) altında
`<SEMBOL>/<S>_<E>.json` desenine uyan, kaydın `[start, end]` aralığını
kapsayan (superset) dosyalar arasından en dar aralıklıyı (birebir eşleşme
otomatik olarak kazanır) otomatik bulur; eşleşme yoksa rapordaki
`price_volume.status` alanı `"not_available"` olur. `--volume-ratio-tolerance`
ve `--adjustment-ratio-epsilon` bu iki parametreyi CLI'dan açıkça ayarlar
(varsayılanları gizli değildir). Rapordaki `price_volume` bölümü provenance
(`source_id`, `accessed_at_utc`, `normalized_sha256`, `unofficial_source`),
tarih bazlı satırlar, kontrol özetleri, `adjustment_boundary_dates` ve
hariç tutulan placeholder tarihlerini taşır. Metin çıktısı yalnızca
geçen/toplam kontrol sayılarını ve placeholder/düzeltme sınırı tarih
sayılarını gösterir, tek tek fiyat/hacim değerlerini listelemez. Toplu mod
indeksi her kayıt için `price_history_found` ve
`price_volume_checks_passed` bayraklarını taşır.

Çalıştırma (ağ ve model çağrısı yok):

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\analyze_period.py data\interim\period_analysis\<SYM>\<START>_<END>.json `
  --price-history data\interim\price_history\<SYM>\<START>_<END>.json
```

`--format json` tam raporu, `--save` ise raporu `<START>_<END>.report.json`
olarak üzerine yazmadan kaydeder. Rapor girdi dosyası SHA-256'sını, varsa kod
commit'ini, Python sürümünü ve `--lags`, `--detail-lag`, `--top` parametrelerini
taşır.

### Toplu mod (`--all`)

`analyze_period.py capture_dosyasi.json` yerine `--all [ROOT]` verilirse (ikisi
birlikte verilemez), `ROOT/<SYMBOL>/<YYYY-MM-DD>_<YYYY-MM-DD>.json` desenine
uyan her kaydı bulur (varsayılan ROOT: `data/interim/period_analysis`);
`*.report.json`, `index.json` ve desene uymayan başka dosyalar yok sayılır.
Her kayıt aynı `--lags`/`--detail-lag`/`--top` ile ayrı ayrı doğrulanır ve
raporlanır; doğrulaması başarısız bir kayıt sabit hata nedeniyle raporlanır ve
diğerlerini durdurmaz. `--save` verilirse her kaydın raporu tek dosyalık moddaki
gibi üzerine yazılmadan kaydedilir (mevcut rapor "skipped" sayılır, hata
değildir).

Çıktı; `--format json` ile sembol, dönem, `server_date_count`, şema sürümü
(v1/v2), AKD mutabakat durum sayıları, takas farkı istisna sayısı, gecikme
özetleri, kurum dizini var/yok ve rapor durumu (`saved`/`skipped`/`not_saved`/
`failed`) içeren kısa bir çapraz-kayıt indeksidir; aksi halde kısa Türkçe metin
özetidir. Tüm sayılar `build_period_report` çıktısından gelir. `--save` ile bu
indeks ayrıca `ROOT/index.json` olarak da yazılır; tek kayıtlık raporların
aksine bu dosya türetilmiş olduğu için her çalıştırmada **üzerine yazılır**.
Herhangi bir kayıt doğrulaması başarısız olursa çıkış kodu 1'dir.

```powershell
.\.venv\Scripts\python.exe scripts\analyze_period.py --all
```
