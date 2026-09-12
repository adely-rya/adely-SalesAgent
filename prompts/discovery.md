指定されたカテゴリをWeb Searchで個別に探索し、質の高い企業を2〜5社程度返す。
目的は映像制作会社を募集している企業の検索ではなく、企業活動から映像需要が
発生するタイミングを推測すること。10カテゴリ合計20〜50社を目指すが水増ししない。
直近30日の発表を優先し、神奈川、東京、首都圏、必要に応じ全国の順で探す。
新ブランド→認知形成→ブランドムービー、新サービス→説明→サービス紹介、
採用強化→企業理解→採用映像、ホテル開業→世界観・施設訴求→ブランド映像を推論する。
新拠点、新施設、飲食店、新商品、資金調達、周年、大型イベント、展示会、海外進出、
新規事業、リブランディング、プロダクトローンチも営業トリガーとする。
公式発表など一次情報を優先し、記事の公開日とイベント予定日を混同しない。
source_urlは今回Web Searchで実際に取得したURLをそのまま使用し、絶対に創作しない。
出典がない候補は返さない。source_titleは実際の記事名。websiteは公式サイトのみ、不明ならnull。
published_atは記事公開日YYYY-MM-DD、不明ならnull。discovered_atはnullでよい。
trigger_typeは brand_launch / store_opening / service_launch / product_launch / funding /
rebranding / hiring / anniversary / hotel_opening / new_business / other から選ぶ。
各候補は入力candidate_schemaに従い、{"candidates": [...]}として返す。
