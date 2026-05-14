$rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
$bytes = New-Object byte[] 18
$rng.GetBytes($bytes)
[System.Convert]::ToBase64String($bytes)
