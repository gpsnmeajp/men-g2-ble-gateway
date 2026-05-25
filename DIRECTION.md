以下を実装してください

+ Pythonで実装してください
+ G2.kt (MentraOS Project) を元に、mentraosフォルダ配下に、Even G2通信用のライブラリを構築してください(LICENSEを明記し、ファイルは主要処理とは分離されている必要があります。)
+ 上記ライブラリを使って、HTTP & WebSocketサーバー(gateway_server.py)を作成してください。

https://github.com/Mentra-Community/MentraOS/blob/dev/mobile/modules/bluetooth-sdk/android/src/main/java/com/mentra/bluetoothsdk/sgcs/G2.kt

gateway_server.pyの仕様

+ HTTPリクエストとして、JSON形式でテキスト、画像(BASE64)、およびそれらの位置構成を受け付けます。受け取ったら、画面に反映します。
+ HTTPリクエストとして、マイクのオンオフを受け付けるようにします。
+ 位置指定なしテキストのみのリクエストの場合は、全面テキストとして高速に構築・反映します。それ以外は、コンテナを構築して画面更新します。
+ WebSocketは、グラスからの全イベント(マイクストリーミング含む)を全クライアントに配信します。
+ gatewayは、TkによるGUIを持ち、待受状態やグラスとの接続状態の表示をします。(引数からno-gui指定もできるようにします。)
+ ポート番号や、接続先グラスの設定はyamlファイルに保存します。一度接続したグラスの情報は保存され、次回から高速に接続できるようにします。
+ グラスが切断されたり、EXITした場合は、速やかに再接続・再度初期化します。
+ BLE接続状態を管理し、Heart Beatを定期的に送信します。
+ 簡易的なフロントエンドをuiフォルダ配下に、HTMLで作成し、静的ファイルとして配信します。

gateway_cli.py

+ テキストや任意の画像ファイルを、サーバーに送信できるようにします。
+ イベントをコンソール上に垂れ流し表示する機能も必要です。
+ デバッグ、AIエージェントからの操作を想定しています。
