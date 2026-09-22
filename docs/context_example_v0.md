# Market context example v0

Bu örnek yalnızca `tests/fixtures/market/` altındaki sanitize fixture'lardan
deterministik olarak üretilmiştir. Canlı bağlantı veya model çağrısı yapılmamıştır.

## Structured context

```json
{
  "schema_version": "market_context_v0",
  "symbol": "GLRMK",
  "analysis_time": "2026-09-22T10:16:00+03:00",
  "data_freshness": {
    "market_as_of": {
      "value": null,
      "available": false
    },
    "depth_as_of": {
      "value": "2026-09-22T10:15:30+03:00",
      "available": true
    },
    "akd_as_of": {
      "value": "2026-09-22",
      "available": true
    },
    "custody_date": {
      "value": "2026-09-22",
      "available": true
    },
    "custody_history_to": {
      "value": "2026-09-22",
      "available": true
    },
    "custody_stats_to": {
      "value": "2026-09-22",
      "available": true
    },
    "recent_trades_as_of": {
      "value": null,
      "available": false
    }
  },
  "market": {
    "available": false,
    "source": null,
    "as_of": {
      "value": null,
      "available": false
    },
    "last_price": {
      "value": null,
      "available": false
    },
    "volume": {
      "value": null,
      "available": false
    }
  },
  "depth": {
    "available": true,
    "source": "depth_update",
    "as_of": {
      "value": "2026-09-22T10:15:30+03:00",
      "available": true
    },
    "raw_summary": {
      "level_limit_per_side": 10,
      "bids": [
        {
          "price": "123.4",
          "lots": 9000000000000,
          "order_count": {
            "value": 12,
            "available": true
          }
        },
        {
          "price": "123.3",
          "lots": 2500,
          "order_count": {
            "value": 8,
            "available": true
          }
        },
        {
          "price": "123.2",
          "lots": 0,
          "order_count": {
            "value": 0,
            "available": true
          }
        }
      ],
      "asks": [
        {
          "price": "123.5",
          "lots": 3100,
          "order_count": {
            "value": 9,
            "available": true
          }
        },
        {
          "price": "123.6",
          "lots": 4200,
          "order_count": {
            "value": 10,
            "available": true
          }
        }
      ]
    },
    "features": {
      "total_bid_lots": {
        "value": 9000000002500,
        "available": true
      },
      "total_ask_lots": {
        "value": 7300,
        "available": true
      },
      "bid_ask_imbalance": {
        "value": "0.9999999983777777795441975289",
        "available": true
      },
      "top5_bid_lots": {
        "value": 9000000002500,
        "available": true
      },
      "top5_ask_lots": {
        "value": 7300,
        "available": true
      },
      "spread": {
        "value": "0.1",
        "available": true
      },
      "spread_pct": {
        "value": "0.08100445524503847711624139328",
        "available": true
      }
    }
  },
  "akd": {
    "available": true,
    "source": "akd_snapshot",
    "as_of": {
      "value": "2026-09-22",
      "available": true
    },
    "market_price": {
      "value": "123.5",
      "available": true
    },
    "broker_limit_per_side": 5,
    "top_buyers": [
      {
        "code": "AAA",
        "buy_lots": 150000,
        "sell_lots": 50000,
        "net_lots": 100000,
        "average_cost": {
          "value": "122.8",
          "available": true
        }
      }
    ],
    "top_sellers": [
      {
        "code": "BBB",
        "buy_lots": 20000,
        "sell_lots": 95000,
        "net_lots": -75000,
        "average_cost": {
          "value": "124.1",
          "available": true
        }
      }
    ],
    "features": {
      "top5_buy_concentration": {
        "value": "1",
        "available": true
      },
      "top5_sell_concentration": {
        "value": "1",
        "available": true
      },
      "broker_cost_vs_market_price": [
        {
          "code": "AAA",
          "gap_pct": {
            "value": "-0.5668016194331983805668016194",
            "available": true
          }
        },
        {
          "code": "BBB",
          "gap_pct": {
            "value": "0.4858299595141700404858299595",
            "available": true
          }
        },
        {
          "code": "CCC",
          "gap_pct": {
            "value": "-100",
            "available": true
          }
        }
      ]
    }
  },
  "custody": {
    "available": true,
    "snapshot": {
      "available": true,
      "source": "custodian_data",
      "date": {
        "value": "2026-09-22",
        "available": true
      },
      "holder_limit": 10,
      "top_holders": [
        {
          "code": "CCC",
          "value": 21474836480,
          "percentage": "69.4"
        },
        {
          "code": "AAA",
          "value": 31500432,
          "percentage": "30.6"
        },
        {
          "code": "BBB",
          "value": 0,
          "percentage": "0.0"
        }
      ]
    },
    "distribution": {
      "available": true,
      "source": "custody_distribution_data",
      "periods": [
        "2026-09-22",
        "2026-09-21",
        "2026-09-15",
        "2026-08-22",
        "2026-06-22"
      ],
      "changes": [
        {
          "code": "AAA",
          "change_1d": 500432,
          "change_7d": 1500432,
          "change_30d": 3500432,
          "change_90d": 6500432
        },
        {
          "code": "BBB",
          "change_1d": -500000,
          "change_7d": -750000,
          "change_30d": -1250000,
          "change_90d": -2000000
        }
      ]
    },
    "history": {
      "available": true,
      "source": "custody_analysis_data",
      "from": {
        "value": "2026-09-20",
        "available": true
      },
      "to": {
        "value": "2026-09-22",
        "available": true
      },
      "raw_daily_points_included": false,
      "persistence": [
        {
          "code": "AAA",
          "direction": {
            "value": "buy",
            "available": true
          },
          "persistence": {
            "value": "1",
            "available": true
          },
          "nonzero_observations": 3
        },
        {
          "code": "BBB",
          "direction": {
            "value": "sell",
            "available": true
          },
          "persistence": {
            "value": "1",
            "available": true
          },
          "nonzero_observations": 2
        }
      ]
    },
    "features": {
      "top5_custody_concentration": {
        "value": "1",
        "available": true
      }
    }
  },
  "custody_stats": {
    "available": true,
    "source": "custody_stats_data",
    "as_of": {
      "value": "2026-09-22",
      "available": true
    },
    "retail_percentage": {
      "value": "41.0",
      "available": true
    },
    "institution_percentage": {
      "value": "59.0",
      "available": true
    },
    "investor_count": {
      "value": 121250,
      "available": true
    },
    "features": {
      "retail_share_change": {
        "value": "-1.5",
        "available": true
      },
      "institutional_share_change": {
        "value": "1.5",
        "available": true
      },
      "investor_count_change": {
        "value": 1250,
        "available": true
      }
    }
  },
  "recent_trades": {
    "available": false,
    "source": null,
    "as_of": {
      "value": null,
      "available": false
    },
    "trade_count": {
      "value": null,
      "available": false
    },
    "total_lots": {
      "value": null,
      "available": false
    },
    "raw_trades_included": false
  },
  "data_availability": {
    "market": false,
    "depth": true,
    "akd": true,
    "custody_snapshot": true,
    "custody_distribution": true,
    "custody_history": true,
    "custody_stats": true,
    "recent_trades": false
  },
  "missing_for_stronger_analysis": [
    "current_market_data",
    "recent_trades"
  ],
  "analysis_constraints": [
    "AKD is not custody.",
    "Broker code does not identify the ultimate investor.",
    "A single-day broker flow does not prove persistent accumulation.",
    "Order-book liquidity can change rapidly.",
    "Missing data must not be inferred.",
    "Conflicting horizons should be reported, not silently averaged.",
    "Do not claim certain future price direction from these observations."
  ]
}
```

## Rendered text context

```text
[ŞEMA]
market_context_v0

[SEMBOL]
GLRMK

[ANALİZ ZAMANI]
2026-09-22T10:16:00+03:00

[VERİ ZAMANLARI]
{"akd_as_of":{"available":true,"value":"2026-09-22"},"custody_date":{"available":true,"value":"2026-09-22"},"custody_history_to":{"available":true,"value":"2026-09-22"},"custody_stats_to":{"available":true,"value":"2026-09-22"},"depth_as_of":{"available":true,"value":"2026-09-22T10:15:30+03:00"},"market_as_of":{"available":false,"value":null},"recent_trades_as_of":{"available":false,"value":null}}

[PİYASA]
{"as_of":{"available":false,"value":null},"available":false,"last_price":{"available":false,"value":null},"source":null,"volume":{"available":false,"value":null}}

[DERİNLİK]
{"as_of":{"available":true,"value":"2026-09-22T10:15:30+03:00"},"available":true,"features":{"bid_ask_imbalance":{"available":true,"value":"0.9999999983777777795441975289"},"spread":{"available":true,"value":"0.1"},"spread_pct":{"available":true,"value":"0.08100445524503847711624139328"},"top5_ask_lots":{"available":true,"value":7300},"top5_bid_lots":{"available":true,"value":9000000002500},"total_ask_lots":{"available":true,"value":7300},"total_bid_lots":{"available":true,"value":9000000002500}},"raw_summary":{"asks":[{"lots":3100,"order_count":{"available":true,"value":9},"price":"123.5"},{"lots":4200,"order_count":{"available":true,"value":10},"price":"123.6"}],"bids":[{"lots":9000000000000,"order_count":{"available":true,"value":12},"price":"123.4"},{"lots":2500,"order_count":{"available":true,"value":8},"price":"123.3"},{"lots":0,"order_count":{"available":true,"value":0},"price":"123.2"}],"level_limit_per_side":10},"source":"depth_update"}

[AKD]
{"as_of":{"available":true,"value":"2026-09-22"},"available":true,"broker_limit_per_side":5,"features":{"broker_cost_vs_market_price":[{"code":"AAA","gap_pct":{"available":true,"value":"-0.5668016194331983805668016194"}},{"code":"BBB","gap_pct":{"available":true,"value":"0.4858299595141700404858299595"}},{"code":"CCC","gap_pct":{"available":true,"value":"-100"}}],"top5_buy_concentration":{"available":true,"value":"1"},"top5_sell_concentration":{"available":true,"value":"1"}},"market_price":{"available":true,"value":"123.5"},"source":"akd_snapshot","top_buyers":[{"average_cost":{"available":true,"value":"122.8"},"buy_lots":150000,"code":"AAA","net_lots":100000,"sell_lots":50000}],"top_sellers":[{"average_cost":{"available":true,"value":"124.1"},"buy_lots":20000,"code":"BBB","net_lots":-75000,"sell_lots":95000}]}

[TAKAS]
{"available":true,"distribution":{"available":true,"changes":[{"change_1d":500432,"change_30d":3500432,"change_7d":1500432,"change_90d":6500432,"code":"AAA"},{"change_1d":-500000,"change_30d":-1250000,"change_7d":-750000,"change_90d":-2000000,"code":"BBB"}],"periods":["2026-09-22","2026-09-21","2026-09-15","2026-08-22","2026-06-22"],"source":"custody_distribution_data"},"features":{"top5_custody_concentration":{"available":true,"value":"1"}},"history":{"available":true,"from":{"available":true,"value":"2026-09-20"},"persistence":[{"code":"AAA","direction":{"available":true,"value":"buy"},"nonzero_observations":3,"persistence":{"available":true,"value":"1"}},{"code":"BBB","direction":{"available":true,"value":"sell"},"nonzero_observations":2,"persistence":{"available":true,"value":"1"}}],"raw_daily_points_included":false,"source":"custody_analysis_data","to":{"available":true,"value":"2026-09-22"}},"snapshot":{"available":true,"date":{"available":true,"value":"2026-09-22"},"holder_limit":10,"source":"custodian_data","top_holders":[{"code":"CCC","percentage":"69.4","value":21474836480},{"code":"AAA","percentage":"30.6","value":31500432},{"code":"BBB","percentage":"0.0","value":0}]}}

[TAKAS İSTATİSTİKLERİ]
{"as_of":{"available":true,"value":"2026-09-22"},"available":true,"features":{"institutional_share_change":{"available":true,"value":"1.5"},"investor_count_change":{"available":true,"value":1250},"retail_share_change":{"available":true,"value":"-1.5"}},"institution_percentage":{"available":true,"value":"59.0"},"investor_count":{"available":true,"value":121250},"retail_percentage":{"available":true,"value":"41.0"},"source":"custody_stats_data"}

[SON İŞLEMLER]
{"as_of":{"available":false,"value":null},"available":false,"raw_trades_included":false,"source":null,"total_lots":{"available":false,"value":null},"trade_count":{"available":false,"value":null}}

[VERİ MEVCUDİYETİ]
{"akd":true,"custody_distribution":true,"custody_history":true,"custody_snapshot":true,"custody_stats":true,"depth":true,"market":false,"recent_trades":false}

[VERİ EKSİKLERİ]
["current_market_data","recent_trades"]

[KISITLAR]
- AKD is not custody.
- Broker code does not identify the ultimate investor.
- A single-day broker flow does not prove persistent accumulation.
- Order-book liquidity can change rapidly.
- Missing data must not be inferred.
- Conflicting horizons should be reported, not silently averaged.
- Do not claim certain future price direction from these observations.
```

## Analysis prompt

### System

```text
Sen Borsa İstanbul piyasa mikroyapısı
verilerini analiz eden bir yardımcısın.

Görevin:
- verilen verileri birlikte yorumlamak,
- destekleyen ve çelişen sinyalleri ayırmak,
- AKD ile Takas'ı karıştırmamak,
- farklı zaman ölçeklerini ayrı değerlendirmek,
- veri eksikse bunun analiz üzerindeki etkisini söylemek,
- mevcut olmayan bilgiyi uydurmamak.

Kesin fiyat yönü, hedef fiyat veya doğrudan al/sat tavsiyesi verme. Context içindeki
deterministik feature sonuçlarını kullan; rakamları yeniden hesaplamaya çalışma.
Kaynakları ve veri zamanlarını gözet. Gözlem, çıkarım ve belirsizliği ayır.
```

### User

```text
Aşağıdaki yapılandırılmış piyasa context'ini Türkçe analiz et.

[ŞEMA]
market_context_v0

[SEMBOL]
GLRMK

[ANALİZ ZAMANI]
2026-09-22T10:16:00+03:00

[VERİ ZAMANLARI]
{"akd_as_of":{"available":true,"value":"2026-09-22"},"custody_date":{"available":true,"value":"2026-09-22"},"custody_history_to":{"available":true,"value":"2026-09-22"},"custody_stats_to":{"available":true,"value":"2026-09-22"},"depth_as_of":{"available":true,"value":"2026-09-22T10:15:30+03:00"},"market_as_of":{"available":false,"value":null},"recent_trades_as_of":{"available":false,"value":null}}

[PİYASA]
{"as_of":{"available":false,"value":null},"available":false,"last_price":{"available":false,"value":null},"source":null,"volume":{"available":false,"value":null}}

[DERİNLİK]
{"as_of":{"available":true,"value":"2026-09-22T10:15:30+03:00"},"available":true,"features":{"bid_ask_imbalance":{"available":true,"value":"0.9999999983777777795441975289"},"spread":{"available":true,"value":"0.1"},"spread_pct":{"available":true,"value":"0.08100445524503847711624139328"},"top5_ask_lots":{"available":true,"value":7300},"top5_bid_lots":{"available":true,"value":9000000002500},"total_ask_lots":{"available":true,"value":7300},"total_bid_lots":{"available":true,"value":9000000002500}},"raw_summary":{"asks":[{"lots":3100,"order_count":{"available":true,"value":9},"price":"123.5"},{"lots":4200,"order_count":{"available":true,"value":10},"price":"123.6"}],"bids":[{"lots":9000000000000,"order_count":{"available":true,"value":12},"price":"123.4"},{"lots":2500,"order_count":{"available":true,"value":8},"price":"123.3"},{"lots":0,"order_count":{"available":true,"value":0},"price":"123.2"}],"level_limit_per_side":10},"source":"depth_update"}

[AKD]
{"as_of":{"available":true,"value":"2026-09-22"},"available":true,"broker_limit_per_side":5,"features":{"broker_cost_vs_market_price":[{"code":"AAA","gap_pct":{"available":true,"value":"-0.5668016194331983805668016194"}},{"code":"BBB","gap_pct":{"available":true,"value":"0.4858299595141700404858299595"}},{"code":"CCC","gap_pct":{"available":true,"value":"-100"}}],"top5_buy_concentration":{"available":true,"value":"1"},"top5_sell_concentration":{"available":true,"value":"1"}},"market_price":{"available":true,"value":"123.5"},"source":"akd_snapshot","top_buyers":[{"average_cost":{"available":true,"value":"122.8"},"buy_lots":150000,"code":"AAA","net_lots":100000,"sell_lots":50000}],"top_sellers":[{"average_cost":{"available":true,"value":"124.1"},"buy_lots":20000,"code":"BBB","net_lots":-75000,"sell_lots":95000}]}

[TAKAS]
{"available":true,"distribution":{"available":true,"changes":[{"change_1d":500432,"change_30d":3500432,"change_7d":1500432,"change_90d":6500432,"code":"AAA"},{"change_1d":-500000,"change_30d":-1250000,"change_7d":-750000,"change_90d":-2000000,"code":"BBB"}],"periods":["2026-09-22","2026-09-21","2026-09-15","2026-08-22","2026-06-22"],"source":"custody_distribution_data"},"features":{"top5_custody_concentration":{"available":true,"value":"1"}},"history":{"available":true,"from":{"available":true,"value":"2026-09-20"},"persistence":[{"code":"AAA","direction":{"available":true,"value":"buy"},"nonzero_observations":3,"persistence":{"available":true,"value":"1"}},{"code":"BBB","direction":{"available":true,"value":"sell"},"nonzero_observations":2,"persistence":{"available":true,"value":"1"}}],"raw_daily_points_included":false,"source":"custody_analysis_data","to":{"available":true,"value":"2026-09-22"}},"snapshot":{"available":true,"date":{"available":true,"value":"2026-09-22"},"holder_limit":10,"source":"custodian_data","top_holders":[{"code":"CCC","percentage":"69.4","value":21474836480},{"code":"AAA","percentage":"30.6","value":31500432},{"code":"BBB","percentage":"0.0","value":0}]}}

[TAKAS İSTATİSTİKLERİ]
{"as_of":{"available":true,"value":"2026-09-22"},"available":true,"features":{"institutional_share_change":{"available":true,"value":"1.5"},"investor_count_change":{"available":true,"value":1250},"retail_share_change":{"available":true,"value":"-1.5"}},"institution_percentage":{"available":true,"value":"59.0"},"investor_count":{"available":true,"value":121250},"retail_percentage":{"available":true,"value":"41.0"},"source":"custody_stats_data"}

[SON İŞLEMLER]
{"as_of":{"available":false,"value":null},"available":false,"raw_trades_included":false,"source":null,"total_lots":{"available":false,"value":null},"trade_count":{"available":false,"value":null}}

[VERİ MEVCUDİYETİ]
{"akd":true,"custody_distribution":true,"custody_history":true,"custody_snapshot":true,"custody_stats":true,"depth":true,"market":false,"recent_trades":false}

[VERİ EKSİKLERİ]
["current_market_data","recent_trades"]

[KISITLAR]
- AKD is not custody.
- Broker code does not identify the ultimate investor.
- A single-day broker flow does not prove persistent accumulation.
- Order-book liquidity can change rapidly.
- Missing data must not be inferred.
- Conflicting horizons should be reported, not silently averaged.
- Do not claim certain future price direction from these observations.

Yanıtında veri kapsamını, destekleyen/çelişen gözlemleri, farklı zaman ufuklarını,
eksik verinin etkisini ve belirsizlikleri açıkça belirt.
```
