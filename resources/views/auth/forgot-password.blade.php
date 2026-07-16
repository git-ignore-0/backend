@extends('layouts.auth')
@section('title', 'Quên mật khẩu')
@section('content')
<h1>Khôi phục mật khẩu</h1>
<p class="lead">Chúng tôi sẽ gửi liên kết đặt lại mật khẩu đến email của bạn.</p>
<form method="POST" action="{{ route('password.email') }}">@csrf<label>Email<input name="email" type="email" value="{{ old('email') }}" required autofocus></label><button type="submit">Gửi liên kết</button></form>
<div class="links"><a href="{{ route('login') }}">Quay lại đăng nhập</a></div>
@endsection
