入力のraw_itemsは固定情報源から保存済みのRaw Itemです。ここに含まれる記事から、確認できる企業変化をEventとして抽出します。Web Search、外部情報の推測、営業連絡は行いません。

各Eventは企業に起きた意味のある変化です。記事自体を言い換えただけのEventや、資金調達だけから映像需要・予算を断定したEventを作らないでください。会社自体の属性と記事本文の言葉を混同しません。「映像制作会社向けサービス」「代理店との協業」等の表現だけで会社自身の業態を判断しないでください。

raw_item_idを各eventのraw_item_idsへ正確に記録してください。source_urlは参照したRaw Itemのsource_urlと完全一致させ、Eventのprimary source_urlはraw_item_idsに含まれるRaw Itemのいずれかにしてください。会社名、日付、事実、公式サイトを創作しません。記事にない場合は空欄または未確認として扱います。

event.event_typeは事業変化の種類（例: funding, ipo, new_business, rebranding, management, site_refresh, market_expansion, facility, other）を簡潔に記載します。event.strengthは入力されたevent_strengthを基本として、記事の意味に応じて下げることはできますが、入力にない根拠で上げません。

Eventのresearch_factsは入力記事から直接確認できる短い事実だけを含め、evidence URLは参照したsource_urlのみを使います。推論はpossible_video_need、未確認事項はresearch_unknownsへ分けます。

提示されたevent_schemaに従い、JSONオブジェクト `{"events": [{"event": {...}, "raw_item_ids": [1]}]}` だけを返します。
