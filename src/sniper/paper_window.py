"""Optional account-bound seven-day paper entry window; exits never call this guard."""
def require_open(trial, account_id, now):
    if trial is None:
        return  # Legacy accounts have their original rules until explicitly enrolled.
    if (trial.get('mode') != 'paper' or trial.get('account_id') != account_id
            or type(trial.get('started_at')) is not int or type(trial.get('ends_at')) is not int
            or trial['ends_at'] - trial['started_at'] != 604800):
        raise ValueError('Invalid account-bound paper trial')
    if not trial['started_at'] <= now < trial['ends_at']:
        raise ValueError('Seven-day paper entry window closed; exits remain active')
