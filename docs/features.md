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
