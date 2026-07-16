@extends('layouts.auth')
@section('title', 'Xác minh email')
@section('content')
<h1>Xác minh email</h1>
<p class="lead">Hãy mở email vừa nhận và bấm liên kết xác minh. Sau đó tài khoản mới có thể đăng nhập vào website và diễn đàn.</p>
<form method="POST" action="{{ route('verification.send') }}">@csrf<button type="submit">Gửi lại email xác minh</button></form>
<form method="POST" action="{{ route('logout') }}" class="secondary-form">@csrf<button class="secondary" type="submit">Đăng xuất</button></form>
@endsection
