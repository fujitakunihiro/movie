# 自宅PC用 動画ストリーミングサーバー

Docker Composeで起動する動画ライブラリです。FFmpegでHLSを生成し、スマートフォンでも再生しやすいH.264/AACに変換します。自動画質選択は通信状況に応じて360p・480p・720pを切り替えます。手動で画質を固定することもできます。

## 起動方法

1. `.env.example` を `.env` にコピーします。
2. `VIDEO_DIR` にライブラリのフォルダ、`MOVIE_DIR` に `videos/movie.lnk` が指す実フォルダを設定します。
3. Docker Desktopを起動し、次を実行します。

```powershell
docker compose up -d --build
```

4. `http://localhost:8080` を開きます。ログインはありません。ポートは `.env` の `PORT` で変更できます。

Windowsの設定例:

```dotenv
VIDEO_DIR=./videos
MOVIE_DIR=D:/work/movie
PORT=8080
```

`videos/movie.lnk` は画面上で `movie` フォルダとして表示され、`MOVIE_DIR` の内容を読み取り専用で配信します。ショートカットのリンク先を変更した場合は、`.env` の `MOVIE_DIR` も更新してコンテナを再作成してください。

## 画質変換

動画を最初に選んだとき、FFmpegが3画質を生成します。変換中は画面に準備中と表示されます。変換後の配信データとサムネイルは `cache` フォルダに保存され、次回は再生成せずに使えます。キャッシュ削除後は再変換が必要です。元動画とキャッシュの両方にディスク容量を使います。

キャッシュを削除する場合は、コンテナを停止してから実行します。

```powershell
docker compose down
Remove-Item .\cache\* -Recurse -Force
docker compose up -d
```

## 外部から使う場合

認証なしでアクセスできるため、URLに到達できる人は動画を閲覧できます。インターネットへ直接公開せず、VPN経由で利用してください。HTTPSだけではアクセス制限になりません。公開範囲を限定できる認証付きリバースプロキシを使う方法もあります。

## 機能

- フォルダ一覧、パンくずリスト、FFmpeg生成の動画サムネイル
- MP4、MKV、WebM、MOVなどの動画一覧
- HLSによる通信状況に応じた画質の自動切替と手動選択（360p / 480p / 720p）
- HTTP Rangeによる元動画の部分配信、シーク
- モバイル向けレイアウト、再生速度選択、全画面表示
- パストラバーサル防止。ライブラリと明示的に読み取り専用マウントした `MOVIE_DIR` の中だけを配信


hls.js is bundled locally under the Apache License 2.0; see app/static/HLS-LICENSE.txt.

